import asyncio
import httpx
import json
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import DEFAULT_SKILL_ROUTER_FALLBACK_SKILL_ID
from .db import AuditRecord, ConfigEntry, Conversation, MessageIdempotency, SessionEvent, SessionLocal, Skill, Tool, purge_expired_audit_data
from .customer_documents import CustomerDocumentClient
from .console_auth import CONSOLE_PASSWORD_CONFIG_KEY, DEFAULT_CONSOLE_PASSWORD
from .llm import LLMAdapter
from .knowledge import KnowledgeGatewayClient
from .ocr import OCRGatewayClient
from .platform import PlatformGatewayClient
from .principal import Principal
from .answer_stream import AnswerStream, incomplete_stream_notice, cancelled_stream_notice
from .customer_answer_stream import customer_generated_answer
from .customer_record_intent import customer_record_intent
from .service_fees import CustomerServiceFeeClient, is_public_service_fee_query, fee_answer
from .application_listing import application_list_answer, application_page_arguments, ordered_application_result
from .profile_scope import ProfileContext, profile_context_from_payload, profile_scope_for_definition, requested_profile, requires_profile_switch
from .response_safety import contains_unexpected_response_script, is_internal_tool_protocol
from .runtime import RuntimeManager
from .skill_router import SkillCatalogCache, add_keyword_skill_candidate, configured_knowledge_fallback, normalized_router_mode, recall_skill_candidates, route_context_from_history, valid_llm_route
from .skill_workflow import build_configured_tool_request, mask_tool_result, matches_configured_selection_follow_up, normalize_route_directives
from .skills import (
    SkillRoute,
    build_flow_prompt,
    build_knowledge_query,
    build_system_prompt,
    canonical_skill_id,
    exact_quote_source_sufficient,
    merged_skill_workflow,
    resolve_configured_skill,
    resolve_skill,
    response_language_for,
)
from .tool_registry import SYSTEM_DEFAULT_TOOL_NAMES, build_legacy_tool_request, system_default_tool_definitions
from .tool_gateway import ToolGateway, parse_tool_request
from .umc_auth import UMCAuthClient


class EventBroker:
    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)

    def subscribe(self, conversation_id: str) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=500)
        self._subscribers[conversation_id].add(queue)
        return queue

    def unsubscribe(self, conversation_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers[conversation_id].discard(queue)

    async def publish(self, conversation_id: str, event: dict[str, Any]) -> None:
        for queue in list(self._subscribers.get(conversation_id, ())):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Slow clients can resume from PostgreSQL using afterSeq.
                pass


class DSHService:
    @staticmethod
    def is_cross_account_record_request(content: str, profile_context: ProfileContext | None) -> bool:
        """Refuse unknown record owners without querying whether they exist."""
        return customer_record_intent(content, profile_context).external_owner

    @staticmethod
    def is_global_profile_record_request(content: str, profile_context: ProfileContext | None) -> bool:
        """Recognize a first-person profile-record query while Global View is active."""

        if not profile_context or not profile_context.is_global_view:
            return False
        intent = customer_record_intent(content, profile_context)
        return intent.query and not intent.external_owner

    @staticmethod
    def is_application_list_request(content: str) -> bool:
        """Recognize read-only My Requests lookups before LLM skill routing.

        Profile words often appear in the same sentence (for example,
        ``Show my applications in the Government UMC profile``).  The
        generic router can otherwise select ``profile_status`` and never
        execute the already-published ``umc.applications`` tool.  Keep this
        detector narrow and limited to the caller's own application records.
        """

        intent = customer_record_intent(content)
        return "applications" in intent.categories and intent.query

    @staticmethod
    def profile_scope_guard(content: str, response_language: str, profile_context: ProfileContext | None) -> dict[str, Any] | None:
        """Return deterministic scope responses before routing or model generation."""

        if is_public_service_fee_query(content):
            # Published service descriptions are not another person's records.
            # This detector rejects mixed/private queries before any exemption.
            return None
        if DSHService.is_cross_account_record_request(content, profile_context):
            return {
                "code": "external_account_lookup",
                "content": (
                    "يمكنني المساعدة فقط في بيانات الملفات المخول لك الوصول إليها حالياً."
                    if response_language == "ar"
                    else "I can only help with data in your currently authorized profiles."
                ),
            }
        requested = requested_profile(content, profile_context)
        if (
            requested
            and profile_context
            and (profile_context.is_global_view or requested.profile_id != profile_context.active_profile_id)
        ):
            kind_label = ({"individual": "ملف شخصي", "establishment": "ملف منشأة"} if response_language == "ar" else {"individual": "individual Profile", "establishment": "establishment Profile"}).get(requested.kind, "الملف" if response_language == "ar" else "Profile")
            return {
                "code": "profile_switch_required",
                "profileAction": {
                    "type": "open_profile_menu",
                    "code": "profile_switch_required",
                    "targetProfile": {
                        "profileId": requested.profile_id,
                        "profileName": requested.name,
                    },
                },
                "content": (
                    f"أنت تستخدم حالياً ملف {profile_context.active_profile_name or 'الحالي'}. يتطلب هذا الطلب {kind_label} {requested.name}. بدّل إلى هذا الملف المخول للمتابعة."
                    if response_language == "ar"
                    else f"You are currently using {profile_context.active_profile_name or 'the current profile'}. This request needs the {kind_label} {requested.name}. Switch to that authorized profile to continue."
                ),
            }
        if DSHService.is_global_profile_record_request(content, profile_context):
            return {
                "code": "profile_selection_required",
                "profileAction": {
                    "type": "open_profile_menu",
                    "code": "profile_selection_required",
                },
                "content": (
                    "أنت حالياً في العرض الشامل. اختر الملف الشخصي أو ملف المنشأة المخول لك من الخيارات أدناه؛ سأتابع هذا السؤال تلقائياً باستخدام نطاق بيانات الملف الذي تختاره."
                    if response_language == "ar"
                    else "You are currently in Global View. Choose your authorized individual or establishment Profile below; I will automatically continue this question using the selected Profile's data scope."
                ),
            }
        return None

    @staticmethod
    def is_profile_sensitive_data_request(content: str) -> bool:
        """Detect direct requests to reveal Profile identifiers or credentials."""

        text = " ".join(str(content or "").casefold().split())
        sensitive_terms = (
            "full id", "id number", "profile id", "identity number", "emirates id",
            "passport number", "license number", "licence number", "document number",
            "access token", "auth token", "authentication token", "authorization token",
            "authorisation token", "token", "authorization", "authorisation", "credential", "secret",
            "رقم الهوية", "رقم الملف", "رقم الجواز", "رقم الترخيص", "رمز الدخول",
            "رمز المصادقة", "بيانات التفويض",
        )
        disclosure_terms = (
            "show", "tell", "display", "give", "reveal", "provide", "what is", "what's",
            "اعرض", "أظهر", "اخبر", "أعطني", "ما هو",
        )
        return any(term in text for term in sensitive_terms) and any(term in text for term in disclosure_terms)

    @staticmethod
    def profile_sensitive_data_refusal(response_language: str) -> str:
        if response_language == "ar":
            return (
                "لحماية حسابك، لا يمكنني عرض أرقام الهوية أو الملف الشخصي أو المنشأة أو الترخيص أو المستندات، "
                "كما لا يمكنني عرض رموز المصادقة أو معلومات التفويض الداخلية في المحادثة. "
                "يمكنك مراجعة بياناتك الرسمية بأمان من صفحة [حسابي](/my-account). "
                "ويمكنني تزويدك بملخص غير حساس يتضمن نوع الملف وحالة المراجعة والصلاحية وتاريخ الانتهاء."
            )
        return (
            "To protect your account, I can't display identity, Profile, establishment, license, or document identifiers, "
            "and I can't reveal authentication tokens or internal authorization details in chat. "
            "You can review your official information securely on the [My Account page](/my-account). "
            "I can still provide a non-sensitive summary such as Profile type, review status, validity, and expiry status."
        )

    def __init__(self, runtime_manager: RuntimeManager, llm: LLMAdapter, broker: EventBroker, ocr: OCRGatewayClient, knowledge: KnowledgeGatewayClient, platform: PlatformGatewayClient) -> None:
        self.runtime_manager = runtime_manager
        self.llm = llm
        self.broker = broker
        self.ocr = ocr
        self.tool_gateway = ToolGateway(ocr, knowledge, platform)
        from .config import get_settings
        self.settings = get_settings()
        self.console_password = DEFAULT_CONSOLE_PASSWORD
        # Environment-provided audit administrator values are a trusted
        # deployment bootstrap. Preserve them separately because persisted
        # runtime config is re-applied during startup and must not be able to
        # shadow the operator's explicit 77 deployment allowlist.
        configured_fields = self.settings.model_fields_set
        self._audit_admin_env_enabled = (
            bool(self.settings.audit_admin_enabled)
            if "audit_admin_enabled" in configured_fields
            else False
        )
        self._audit_admin_env_user_ids = (
            str(self.settings.audit_admin_user_ids or "")
            if "audit_admin_user_ids" in configured_fields
            else ""
        )
        self.documents = CustomerDocumentClient(self.settings)
        self.umc_auth = UMCAuthClient(self.settings)
        self.skill_catalog = SkillCatalogCache(self.settings.redis_url, self.settings.skill_cache_namespace)
        self._turn_tasks: dict[str, asyncio.Task[None]] = {}
        self._answer_streams: dict[str, tuple[AnswerStream, str]] = {}
        self._writer_locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def _config_value(item: ConfigEntry) -> Any:
        value = item.value
        if isinstance(value, dict) and "value" in value and len(value) == 1:
            return value["value"]
        return value

    async def apply_config_entries(self, entries: list[ConfigEntry]) -> None:
        """Apply safe, live-editable config values to the running clients.

        Database/Redis URLs are intentionally not hot-swapped: SQLAlchemy and
        Redis pools are created at process start and require a container
        restart. All other fields in the console can be used immediately for
        subsequent turns and tool calls.
        """

        restart_only = {"database_url", "redis_url", "database_init_enabled", "audit_cleanup_enabled", "skill_cache_namespace", "knowledge_gateway_url", "platform_gateway_url", "ocr_gateway_url"}
        numeric = {
            "llm_timeout_seconds": float,
            "knowledge_timeout_seconds": float,
            "knowledge_retry_attempts": int,
            "knowledge_top_k": int,
            "platform_timeout_seconds": float,
            "ocr_timeout_seconds": float,
            "umc_document_timeout_seconds": float,
            "audit_retention_days": int,
            "audit_cleanup_interval_seconds": int,
            "skill_router_timeout_seconds": float,
        }
        bool_keys = {"external_tools_enabled", "audit_admin_enabled"}
        umc_auth_keys = {
            "umc_portal",
            "umc_customer_base_url",
            "umc_admin_base_url",
            "umc_public_base_url",
            "umc_login_url",
            "umc_login_email",
            "umc_login_password",
        }
        umc_auth_changed = False
        for item in entries:
            key = item.key
            if key == CONSOLE_PASSWORD_CONFIG_KEY:
                value = self._config_value(item)
                if isinstance(value, str) and value:
                    self.console_password = value
                continue
            if key in restart_only or not hasattr(self.settings, key):
                continue
            value = self._config_value(item)
            if value is None or (value == "" and key != "system_prompt"):
                continue
            try:
                if key in numeric:
                    value = numeric[key](value)
                elif key in bool_keys and isinstance(value, str):
                    value = value.strip().lower() in {"1", "true", "yes", "on", "是"}
                elif key == "umc_portal":
                    value = str(value).strip().lower()
                    if value not in {"customer", "admin", "public"}:
                        continue
            except (TypeError, ValueError):
                continue
            if key in umc_auth_keys and getattr(self.settings, key) != value:
                umc_auth_changed = True
            if key == "skill_router_mode":
                value = normalized_router_mode(value)
            setattr(self.settings, key, value)

        # Keep the already-instantiated gateway clients aligned with the
        # effective config. They all read these attributes for the next call.
        self.llm.settings = self.settings
        knowledge = self.tool_gateway.knowledge
        knowledge.base_url = self.settings.knowledge_gateway_url.rstrip("/")
        knowledge.timeout = self.settings.knowledge_timeout_seconds
        knowledge.retry_attempts = max(1, int(self.settings.knowledge_retry_attempts))
        platform = self.tool_gateway.platform
        platform.base_url = self.settings.platform_gateway_url.rstrip("/")
        platform.timeout = self.settings.platform_timeout_seconds
        self.ocr.base_url = self.settings.ocr_gateway_url.rstrip("/")
        self.ocr.timeout = self.settings.ocr_timeout_seconds
        self.documents.base_url = self.settings.umc_document_service_base_url.rstrip("/")
        self.documents.timeout = self.settings.umc_document_timeout_seconds
        if umc_auth_changed:
            self.umc_auth.invalidate()

    @staticmethod
    def route_shape_for_skill(skill_id: str, catalog: list[dict[str, Any]], *, routing_locked: bool = False):
        """Derive automatic execution only from a Skill's allowed Tools."""

        item = next((candidate for candidate in catalog if candidate.get("skillId") == skill_id), None)
        allowed_tools = set(item.get("allowedTools", [])) if item else set()
        business_tools = allowed_tools - SYSTEM_DEFAULT_TOOL_NAMES
        if allowed_tools == {"knowledge.search"}:
            return SkillRoute(skill_id, "knowledge", "knowledge.search", "summary", routing_locked=routing_locked)
        if business_tools or allowed_tools:
            return SkillRoute(skill_id, "api_call", None, "answer", routing_locked=routing_locked)
        return SkillRoute(skill_id, "data_query", None, "answer", routing_locked=routing_locked)

    @staticmethod
    def attachment_ocr_route(attachment: dict[str, Any] | None) -> SkillRoute | None:
        """Lock valid uploaded attachments to the document-OCR Skill.

        An attachment is customer-provided evidence.  It must be analyzed
        before an LLM router or a knowledge fallback can reinterpret the
        accompanying text as an unrelated question.
        """

        if not isinstance(attachment, dict) or not str(attachment.get("fileRef") or "").strip():
            return None
        return SkillRoute("document_ocr", "api_call", None, "answer", routing_locked=True)

    @staticmethod
    def attachment_ocr_reference_hints(ocr_result: dict[str, Any]) -> list[str]:
        """Extract bounded identifiers locally; never send OCR text to the router."""

        raw = json.dumps(ocr_result.get("result", {}), ensure_ascii=False)
        patterns = (
            r"\b(?:MC|HC|ML)-\d+(?:-\d+){1,5}\b",
            r"\b(?:application|refund|enquiry|complaint|appeal|violation)\s*(?:no\.?|number|id)?\s*[:#-]?\s*([A-Z]{1,6}-?\d{3,}(?:-\d+)*)\b",
            r"\b[A-Z]{2,8}-\d{3,}(?:-\d+){1,5}\b",
        )
        hints: list[str] = []
        for pattern in patterns:
            for match in re.finditer(pattern, raw, flags=re.IGNORECASE):
                value = next((part for part in match.groups() if part), match.group(0)).strip()
                if value and value.upper() not in {item.upper() for item in hints}:
                    hints.append(value[:64])
        return hints[:12]

    @staticmethod
    def attachment_ocr_handoff_text(content: str, ocr_result: dict[str, Any]) -> str:
        """Provide local Tool argument matching with text plus safe OCR references."""

        hints = DSHService.attachment_ocr_reference_hints(ocr_result)
        return " ".join(part for part in [content.strip(), *hints] if part).strip()

    @staticmethod
    def attachment_ocr_explicit_skill(content: str, catalog: list[dict[str, Any]]) -> str | None:
        """Choose a published business domain from explicit user words only.

        This is deliberately local and conservative: it is used only after a
        successful OCR pass and never receives OCR text.  The first matching
        rule wins, so more specific read-only domains precede broad ones.
        """

        text = " ".join(content.casefold().split())
        rules: tuple[tuple[str, tuple[str, ...]], ...] = (
            ("application_payment_details", ("application", "payment")),
            ("fine_appeal", ("appeal",)),
            ("fine_appeal", ("申诉",)),
            ("fine_payment", ("fine", "payment")),
            ("fine_payment", ("罚款", "支付")),
            ("refund_status", ("refund",)),
            ("refund_status", ("退款",)),
            ("complaints_status", ("complaint",)),
            ("complaints_status", ("投诉",)),
            ("enquiry_status", ("enquiry",)),
            ("enquiry_status", ("咨询",)),
            ("my_requests_pending_actions", ("pending action",)),
            ("my_requests_pending_actions", ("待处理",)),
            ("application_status", ("application",)),
            ("application_status", ("申请",)),
            ("profile_status", ("profile",)),
            ("profile_status", ("身份",)),
            ("license_renewal", ("renew",)),
            ("license_renewal", ("续期",)),
            ("license_permit_status", ("license",)),
            ("license_permit_status", ("许可证",)),
            ("license_permit_status", ("permit",)),
            ("violations_fines_status", ("violation",)),
            ("violations_fines_status", ("违规",)),
            ("payment_transaction_history", ("payment",)),
            ("payment_transaction_history", ("付款",)),
            ("service_eligibility", ("eligibility",)),
            ("service_eligibility", ("资格",)),
            ("umc_book_by_isbn", ("isbn",)),
        )
        published = {str(item.get("skillId") or "") for item in catalog}
        for skill_id, terms in rules:
            if skill_id in published and all(term in text for term in terms):
                return skill_id
        configured = resolve_configured_skill(content, catalog, canonicalize=False)
        if configured and configured.skill_id in published:
            return configured.skill_id
        return None

    @staticmethod
    def is_read_only_tool_definition(definition: dict[str, Any] | None) -> bool:
        """The OCR handoff must never unlock a confirmation or write Tool."""

        return bool(definition) and str(definition.get("sideEffect") or "read") == "read" and not bool(
            definition.get("confirmationRequired", False)
        )

    @staticmethod
    def attachment_ocr_event_result(ocr_result: dict[str, Any]) -> dict[str, Any]:
        """Create an audit-safe OCR result without persisting document text.

        OCR is performed inside the DSH deployment.  The extracted text can
        contain personal information, so it must not be copied into session
        events or LLM audit records.  References are intentionally retained:
        they are bounded, useful for subsequent read-only lookups, and are
        the only OCR-derived values that cross the OCR boundary.
        """

        return {
            "ok": bool(ocr_result.get("ok")),
            "toolName": str(ocr_result.get("toolName") or "ocr.layout_parsing"),
            "code": str(ocr_result.get("code") or ""),
            "ocrProcessedLocally": True,
            "referenceHints": DSHService.attachment_ocr_reference_hints(ocr_result),
        }

    @staticmethod
    def _attachment_safe_business_value(value: Any, *, depth: int = 0) -> Any:
        """Keep a compact displayable subset of a read-only Tool response.

        The deny-list is deliberately applied before rendering.  It prevents
        a tool response from accidentally carrying attachment text, file data,
        credentials, or binary material into a local assistant answer.
        """

        if depth > 4:
            return None
        if isinstance(value, dict):
            blocked_fragments = (
                "attachment", "authorization", "base64", "binary", "blob",
                "content", "document", "file", "html", "image", "markdown",
                "ocr", "password", "raw", "secret", "text", "token",
            )
            rendered: dict[str, Any] = {}
            for key, nested in value.items():
                normalized = re.sub(r"[^a-z0-9]", "", str(key).casefold())
                if any(fragment in normalized for fragment in blocked_fragments):
                    continue
                safe = DSHService._attachment_safe_business_value(nested, depth=depth + 1)
                if safe not in (None, "", [], {}):
                    rendered[str(key)[:80]] = safe
                if len(rendered) >= 24:
                    break
            return rendered
        if isinstance(value, list):
            items = [DSHService._attachment_safe_business_value(item, depth=depth + 1) for item in value[:12]]
            return [item for item in items if item not in (None, "", [], {})]
        if isinstance(value, str):
            return " ".join(value.split())[:500]
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        return str(value)[:500]

    @staticmethod
    def attachment_ocr_local_response(
        *,
        response_language: str,
        ocr_result: dict[str, Any],
        handoff_skill_id: str | None = None,
        handoff_result: dict[str, Any] | None = None,
    ) -> str:
        """Render an attachment response without invoking the answer LLM."""

        hints = DSHService.attachment_ocr_reference_hints(ocr_result)
        references = ", ".join(hints) if hints else "none"
        is_arabic = response_language == "ar"
        if not handoff_skill_id:
            return (
                "تم تحليل الملف محلياً. ولحماية بياناتك، لم يتم إرسال نص المستند إلى أي نموذج لغوي خارجي. "
                f"المراجع التي تم العثور عليها: {references}. اطرح سؤالاً محدداً للقراءة فقط، مثل حالة طلب الاسترداد أو الطلب أو الشكوى."
                if is_arabic
                else "The document was analysed locally. To protect your data, its text was not sent to any external language model. "
                f"Reference identifiers found: {references}. Ask a specific read-only question, for example about a refund, application, or complaint status."
            )

        if not handoff_result or not handoff_result.get("ok"):
            return (
                "تم تحليل الملف محلياً، لكن تعذر إكمال الاستعلام للقراءة فقط المرتبط بطلبك. لم يتم إرسال نص المستند إلى أي نموذج لغوي خارجي."
                if is_arabic
                else "The document was analysed locally, but the related read-only lookup could not be completed. No document text was sent to any external language model."
            )

        safe_result = DSHService._attachment_safe_business_value(handoff_result.get("result", handoff_result))
        details = json.dumps(safe_result, ensure_ascii=False, indent=2)[:6_000] if safe_result not in (None, "", [], {}) else "No displayable fields were returned."
        return (
            "تم تحليل الملف محلياً، ثم تم تنفيذ استعلام للقراءة فقط مرتبط بطلبك. لم يتم إرسال نص المستند إلى أي نموذج لغوي خارجي.\n\n"
            f"المراجع المطابقة: {references}\n\nالنتيجة المتاحة:\n{details}"
            if is_arabic
            else "The document was analysed locally, then a related read-only lookup was completed. No document text was sent to any external language model.\n\n"
            f"Matched references: {references}\n\nAvailable result:\n{details}"
        )

    @staticmethod
    def answer_tool_evidence(tool_name: str, tool_result: dict[str, Any], masking_policy: object, profile_context: ProfileContext | None = None) -> str:
        """Build bounded, masked evidence for the answer-generation model.

        Tool execution and answer drafting are separate stages.  The answer
        model must receive the successful read result; otherwise it can only
        guess whether the caller has records.  This keeps configured masking
        in force and removes credentials, contact data, attachments and raw
        document content before the result crosses that boundary.
        """

        masked = mask_tool_result(tool_result, masking_policy)
        blocked_fragments = (
            "address", "attachment", "authorization", "base64", "binary",
            "blob", "document", "email", "emirates", "identity",
            "mobile", "password", "passport", "phone", "secret", "token",
        )

        list_limit = 100 if tool_name == "umc.applications" else 20
        truncated = False

        def compact(value: Any, depth: int = 0) -> Any:
            nonlocal truncated
            if depth > 6:
                return None
            if isinstance(value, dict):
                result: dict[str, Any] = {}
                for key, nested in value.items():
                    normalized = re.sub(r"[^a-z0-9]", "", str(key).casefold())
                    if any(fragment in normalized for fragment in blocked_fragments):
                        continue
                    # Internal relational keys are not public document references.
                    # Keep the displayable application/certificate numbers instead.
                    if normalized in {"id", "ids"} or re.search(r"(?:[_ -]ids?$|(?:Id|ID)s?$)", str(key)):
                        continue
                    safe = compact(nested, depth + 1)
                    if safe not in (None, "", [], {}):
                        result[str(key)[:80]] = safe
                    if len(result) >= 40:
                        break
                return result
            if isinstance(value, list):
                truncated = truncated or len(value) > list_limit
                return [safe for item in value[:list_limit] if (safe := compact(item, depth + 1)) not in (None, "", [], {})]
            if isinstance(value, str):
                try:
                    decoded = json.loads(value)
                except (TypeError, ValueError):
                    return " ".join(value.split())[:1_000]
                return compact(decoded, depth + 1)
            if isinstance(value, (int, float, bool)) or value is None:
                return value
            return str(value)[:1_000]

        source = {
                "toolName": tool_name,
                "ok": bool(masked.get("ok")),
                "code": masked.get("code"),
                "status": masked.get("status"),
                "result": ordered_application_result(masked.get("result")) if tool_name == "umc.applications" else masked.get("result"),
            }
        # The current portal identity was bound to the token's Profile claim.
        # Its label is scope metadata, not an inferred field of each record.
        if profile_context and not profile_context.is_global_view:
            source["currentPortalProfile"] = {"name": profile_context.active_profile_name, "scope": "selected_profile_only"}
        for list_limit in dict.fromkeys(min(list_limit, limit) for limit in (100, 50, 20, 10, 5, 1)):
            truncated = False
            evidence = compact(source)
            evidence["evidenceTruncated"] = truncated
            serialized = json.dumps(evidence, ensure_ascii=False)
            if len(serialized) <= 16_000:
                return serialized
        return json.dumps({"toolName": tool_name, "ok": bool(masked.get("ok")), "evidenceTruncated": True, "displayableResultUnavailable": True})

    @staticmethod
    def violation_evidence_keys(tool_result: dict[str, Any]) -> list[str]:
        """Return bounded document keys reported by a violation-detail lookup.

        The Customer Portal violation API returns storage object keys in
        ``reportedViolations[].evidenceUrls``. They are not customer-visible
        filenames, so the runtime must resolve them before answer drafting.
        """

        result: Any = tool_result.get("result") if isinstance(tool_result, dict) else None
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except (TypeError, ValueError):
                return []
        if not isinstance(result, dict):
            return []
        data = result.get("data")
        if not isinstance(data, dict):
            return []

        keys: list[str] = []
        for item in data.get("reportedViolations") or []:
            if not isinstance(item, dict):
                continue
            for value in item.get("evidenceUrls") or []:
                key = str(value or "").strip()
                if key and key not in keys:
                    keys.append(key)
                if len(keys) >= 10:
                    return keys
        return keys

    @staticmethod
    def original_document_name_evidence(tool_result: dict[str, Any]) -> str:
        """Build only the customer-visible name evidence for answer drafting."""

        result: Any = tool_result.get("result") if isinstance(tool_result, dict) else None
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except (TypeError, ValueError):
                return "[]"
        data = result.get("data") if isinstance(result, dict) else None
        names: list[dict[str, str]] = []
        if isinstance(data, list):
            for item in data[:10]:
                if not isinstance(item, dict):
                    continue
                original_name = str(item.get("originalFileName") or "").strip()
                if original_name:
                    names.append({"originalFileName": original_name})
        return json.dumps(names, ensure_ascii=False)

    async def choose_skill_route(
        self,
        db: AsyncSession,
        question: str,
        keyword_route,
        conversation: Conversation,
        request_id: str,
        context: dict[str, Any] | None = None,
    ) -> tuple[Any, dict[str, Any]]:
        mode = normalized_router_mode(getattr(self.settings, "skill_router_mode", "llm"))
        metadata: dict[str, Any] = {"routerMode": mode, "keywordSkillId": keyword_route.skill_id}
        if mode == "keyword":
            return keyword_route, metadata
        if keyword_route.routing_locked:
            # A deterministic route may opt out of LLM replacement when its
            # declared precedence is part of the business contract.
            metadata["routingLocked"] = True
            return keyword_route, metadata

        catalog = await self.skill_catalog.load(db)
        recall = recall_skill_candidates(question, catalog, context)
        recall = add_keyword_skill_candidate(recall, catalog, keyword_route.skill_id)
        candidates = recall.candidates
        candidate_ids = [str(item.get("skillId")) for item in candidates]
        metadata["candidateSkillIds"] = candidate_ids
        metadata["candidateDomainIds"] = recall.domains
        metadata["domainScores"] = recall.scores
        metadata["routeContextUsed"] = bool((context or {}).get("recentMessages") or (context or {}).get("activeSkillId"))
        llm_result: dict[str, object] | None = None
        fallback_reason = ""
        if not candidates:
            valid = False
            fallback_reason = "domain_unresolved"
        else:
            try:
                llm_result = await self.llm.route_skill(question, candidates, context)
                valid, fallback_reason = valid_llm_route(llm_result, candidates)
            except Exception:
                valid = False
                fallback_reason = "router_unavailable"
        llm_skill_id = llm_result.get("skillId") if isinstance(llm_result, dict) else None
        metadata.update(
            {
                "llmSkillId": llm_skill_id,
                "confidence": llm_result.get("confidence") if isinstance(llm_result, dict) else None,
                "needsClarification": bool(llm_result.get("needsClarification", False)) if isinstance(llm_result, dict) else False,
                "routeConsistent": bool(llm_skill_id and llm_skill_id == keyword_route.skill_id),
            }
        )
        if mode == "shadow":
            await self.append_audit(
                db,
                conversation,
                "skill.route.shadow",
                {**metadata, "fallbackReason": fallback_reason or None},
                request_id=request_id,
                runtime_id=conversation.runtime_id,
            )
            return keyword_route, metadata
        if valid and isinstance(llm_skill_id, str):
            selected_candidate = next((item for item in candidates if item.get("skillId") == llm_skill_id), {})
            intent_id, filters = normalize_route_directives(
                {"routing": selected_candidate.get("routing")},
                llm_result.get("intentId") if llm_result else None,
                llm_result.get("filters") if llm_result else None,
            )
            metadata["intentId"] = intent_id
            metadata["filters"] = filters
            return self.route_shape_for_skill(llm_skill_id, catalog), metadata
        metadata["fallbackReason"] = fallback_reason or "invalid_output"
        active_skill_id = str((context or {}).get("activeSkillId") or "")
        if active_skill_id and active_skill_id in candidate_ids:
            # The lexical domain gate has already confined the question to the
            # active business domain. Keep that business context when the LLM
            # router is temporarily unavailable instead of querying the global
            # knowledge fallback for a personal-record follow-up.
            metadata["fallbackSkillId"] = active_skill_id
            return self.route_shape_for_skill(active_skill_id, catalog), metadata
        fallback = configured_knowledge_fallback(
            catalog,
            getattr(self.settings, "skill_router_fallback_skill_id", DEFAULT_SKILL_ROUTER_FALLBACK_SKILL_ID),
        )
        if fallback:
            fallback_id = str(fallback["skillId"])
            metadata["fallbackSkillId"] = fallback_id
            return self.route_shape_for_skill(fallback_id, catalog), metadata
        return SkillRoute("general", "general"), metadata

    @staticmethod
    def _nested_value(value: Any, path: str) -> Any:
        """Read a dotted field from a Tool result without evaluating input."""

        current = value
        for part in (path or "").split("."):
            if not part:
                continue
            if not isinstance(current, dict):
                return None
            current = current.get(part)
        return current

    @staticmethod
    def _tool_result_payload(event_json: dict[str, Any]) -> dict[str, Any] | None:
        raw = event_json.get("result")
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            try:
                decoded = json.loads(raw)
            except (TypeError, ValueError):
                return None
            return decoded if isinstance(decoded, dict) else None
        return None

    def configured_cross_skill_handoff(
        self,
        active_skill: Skill,
        latest_content: str,
        history: list[SessionEvent],
        catalog: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Resolve a published cross-Skill handoff from the latest detail result.

        Handoffs are declarative Skill workflow data.  This keeps Refund and
        Complaints independent while allowing either one to delegate a clearly
        linked application question to the existing My Requests Skill.
        """

        workflow = merged_skill_workflow(active_skill.skill_id, active_skill.workflow)
        handoffs = workflow.get("crossSkillHandoffs") or []
        question = latest_content.casefold().strip()
        for handoff in handoffs:
            if not isinstance(handoff, dict):
                continue
            triggers = handoff.get("triggers") or {}
            any_terms = [str(term).casefold() for term in triggers.get("anyTerms") or [] if str(term).strip()]
            all_terms = [str(term).casefold() for term in triggers.get("allTerms") or [] if str(term).strip()]
            none_terms = [str(term).casefold() for term in triggers.get("noneTerms") or [] if str(term).strip()]
            exact_terms = {str(term).casefold().strip() for term in triggers.get("exactTerms") or [] if str(term).strip()}
            # A one-word detail follow-up such as "application" is safe only
            # inside the active Refund/Complaints detail context.  Keep it an
            # exact match so "refund application" cannot accidentally leave
            # the current Skill.
            exact_match = question in exact_terms
            if not exact_match:
                if any_terms and not any(term in question for term in any_terms):
                    continue
                if all_terms and not all(term in question for term in all_terms):
                    continue
            if any(term in question for term in none_terms):
                continue
            source_tools = {str(value) for value in handoff.get("sourceTools") or [] if str(value).strip()}
            source_event = next(
                (
                    event
                    for event in reversed(history)
                    if event.event_type == "tool.result"
                    and str(event.event_json.get("toolName") or "") in source_tools
                    and event.event_json.get("ok") is True
                ),
                None,
            )
            if not source_event:
                return {
                    "id": str(handoff.get("id") or "related-application"),
                    "targetSkillId": str(handoff.get("targetSkillId") or "application_status"),
                    "sourceSkillId": active_skill.skill_id,
                    "missing": True,
                    "missingMessage": str(handoff.get("missingMessage") or "Please open the specific record first so I can find its related application."),
                }
            payload = self._tool_result_payload(source_event.event_json)
            for extractor in handoff.get("extractors") or []:
                if not isinstance(extractor, dict) or not payload:
                    continue
                value = self._nested_value(payload, str(extractor.get("path") or ""))
                if value in (None, ""):
                    continue
                value_type = str(extractor.get("argumentType") or "string")
                if value_type == "integer":
                    try:
                        value = int(value)
                    except (TypeError, ValueError):
                        continue
                target_skill_id = str(handoff.get("targetSkillId") or "application_status")
                target_entry = next((item for item in catalog if str(item.get("skillId") or "") == target_skill_id), None)
                allowed_tools = set((target_entry or {}).get("allowedTools") or [])
                tool_name = str(extractor.get("toolName") or "")
                argument_name = str(extractor.get("argumentName") or "")
                if not target_entry or tool_name not in allowed_tools or not argument_name:
                    continue
                arguments = dict(extractor.get("arguments") or {})
                arguments[argument_name] = value
                return {
                    "id": str(handoff.get("id") or "related-application"),
                    "targetSkillId": target_skill_id,
                    "sourceSkillId": active_skill.skill_id,
                    "sourceToolName": str(source_event.event_json.get("toolName") or ""),
                    "toolRequest": (tool_name, arguments),
                    "missing": False,
                }
            return {
                "id": str(handoff.get("id") or "related-application"),
                "targetSkillId": str(handoff.get("targetSkillId") or "application_status"),
                "sourceSkillId": active_skill.skill_id,
                "sourceToolName": str(source_event.event_json.get("toolName") or ""),
                "missing": True,
                "missingMessage": str(handoff.get("missingMessage") or "I could not find an application identifier linked to that record. Please provide the application number."),
            }
        return None

    async def parse_document(self, file: str, file_type: int | None, options: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self.ocr.layout_parsing(file, file_type=file_type, options=options)

    def writer_lock_for(self, conversation_id: str) -> asyncio.Lock:
        return self._writer_locks.setdefault(conversation_id, asyncio.Lock())

    async def create_conversation(self, db: AsyncSession, principal: Principal, workspace: str, skill_profile: str, runtime_profile: str) -> Conversation:
        conversation = Conversation(
            conversation_id=f"conv_{uuid4().hex[:20]}",
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            owner_account=principal.audit_account.strip()[:300] or None,
            dsh_session_id=f"dsh_{uuid4().hex[:20]}",
            runtime_profile=runtime_profile,
            workspace=workspace,
            skill_profile=skill_profile,
            status="READY",
            last_seq=0,
            last_activity_at=datetime.now(timezone.utc),
        )
        db.add(conversation)
        await db.commit()
        await db.refresh(conversation)
        return conversation

    async def get_owned_conversation(self, db: AsyncSession, principal: Principal, conversation_id: str) -> Conversation:
        result = await db.execute(
            select(Conversation).where(
                Conversation.conversation_id == conversation_id,
                Conversation.tenant_id == principal.tenant_id,
                Conversation.user_id == principal.user_id,
            )
        )
        conversation = result.scalar_one_or_none()
        if not conversation:
            raise LookupError("conversation not found")
        return conversation

    async def list_owned_conversations(self, db: AsyncSession, principal: Principal) -> list[Conversation]:
        result = await db.execute(
            select(Conversation)
            .where(
                Conversation.tenant_id == principal.tenant_id,
                Conversation.user_id == principal.user_id,
            )
            .order_by(Conversation.last_activity_at.desc(), Conversation.id.desc())
        )
        return list(result.scalars().all())

    def can_view_all_audit(self, principal: Principal) -> bool:
        """Return whether this principal has the explicitly configured audit scope.

        The gateway does not currently pass a verifiable UMC role claim to
        DSH, so audit administrator access is intentionally an explicit
        deployment setting rather than an inference from the selected portal
        or a browser-provided header. A wildcard is supported only for an
        isolated administrator console; specific UMC user IDs are preferred.
        """

        enabled = bool(self.settings.audit_admin_enabled) or getattr(self, "_audit_admin_env_enabled", False)
        if not enabled:
            return False
        configured = ",".join(
            value
            for value in (
                str(self.settings.audit_admin_user_ids or ""),
                getattr(self, "_audit_admin_env_user_ids", ""),
            )
            if value
        )
        allowed_ids = {item.strip() for item in configured.split(",") if item.strip()}
        return "*" in allowed_ids or principal.user_id in allowed_ids

    async def list_audit_conversations(self, db: AsyncSession, principal: Principal) -> tuple[list[Conversation], bool]:
        """List conversations for the audit UI using the narrowest permitted scope."""

        if self.can_view_all_audit(principal):
            result = await db.execute(
                select(Conversation).order_by(Conversation.last_activity_at.desc(), Conversation.id.desc())
            )
            return list(result.scalars().all()), True
        return await self.list_owned_conversations(db, principal), False

    async def get_audit_conversation(self, db: AsyncSession, principal: Principal, conversation_id: str) -> tuple[Conversation, bool]:
        """Resolve an audit target while preserving owner checks for normal users."""

        is_admin = self.can_view_all_audit(principal)
        if is_admin:
            result = await db.execute(
                select(Conversation).where(Conversation.conversation_id == conversation_id)
            )
            conversation = result.scalar_one_or_none()
            if conversation:
                return conversation, True
            raise LookupError("conversation not found")
        return await self.get_owned_conversation(db, principal, conversation_id), False

    async def delete_owned_conversation(
        self,
        db: AsyncSession,
        principal: Principal,
        conversation_id: str,
    ) -> None:
        conversation = await self.get_owned_conversation(db, principal, conversation_id)
        task = self._turn_tasks.pop(conversation_id, None)
        if task and not task.done():
            task.cancel()
        await self.runtime_manager.release(conversation_id)
        await db.execute(
            delete(MessageIdempotency).where(
                MessageIdempotency.conversation_id == conversation_id,
            )
        )
        await db.execute(delete(SessionEvent).where(SessionEvent.conversation_id == conversation_id))
        await db.execute(delete(AuditRecord).where(AuditRecord.conversation_id == conversation_id))
        await db.delete(conversation)
        await db.commit()

    @staticmethod
    def conversation_json(conversation: Conversation, runtime_state: str | None = None) -> dict[str, Any]:
        return {
            "conversationId": conversation.conversation_id,
            "dshSessionId": conversation.dsh_session_id,
            "ownerAccount": conversation.owner_account,
            "workspace": conversation.workspace,
            "skillProfile": conversation.skill_profile,
            "runtimeProfile": conversation.runtime_profile,
            "runtimeId": conversation.runtime_id,
            "runtimeState": runtime_state or conversation.status,
            "status": conversation.status,
            "lastSeq": conversation.last_seq,
            "lastActivityAt": conversation.last_activity_at.isoformat() if conversation.last_activity_at else None,
            "createdAt": conversation.created_at.isoformat() if conversation.created_at else None,
            "lastError": conversation.last_error,
        }

    async def list_events(self, db: AsyncSession, conversation: Conversation, after_seq: int = 0, event_type: str | None = None) -> list[SessionEvent]:
        query = select(SessionEvent).where(SessionEvent.conversation_id == conversation.conversation_id, SessionEvent.seq > after_seq).order_by(SessionEvent.seq)
        if event_type:
            query = query.where(SessionEvent.event_type == event_type)
        return list((await db.execute(query)).scalars().all())

    async def append_event(self, db: AsyncSession, conversation: Conversation, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        active = getattr(self, "_answer_streams", {}).get(conversation.conversation_id)
        if event_type == "assistant.message" and active:
            stream, _ = active
            await stream.verified(str(payload.get("content") or ""))
            payload = {**payload, "streamVersion": stream.protocol_version,
                       "streamStatus": stream.status}
        conversation.last_seq += 1
        conversation.last_activity_at = datetime.now(timezone.utc)
        event = SessionEvent(
            tenant_id=conversation.tenant_id,
            user_id=conversation.user_id,
            conversation_id=conversation.conversation_id,
            dsh_session_id=conversation.dsh_session_id,
            seq=conversation.last_seq,
            event_type=event_type,
            event_json=payload,
        )
        db.add(event)
        request_id = str(payload.get("requestId") or "")[:128] or None
        runtime_id = str(payload.get("runtimeId") or "")[:128] or None
        db.add(
            AuditRecord(
                tenant_id=conversation.tenant_id,
                user_id=conversation.user_id,
                conversation_id=conversation.conversation_id,
                dsh_session_id=conversation.dsh_session_id,
                request_id=request_id,
                runtime_id=runtime_id,
                category=self.audit_category(event_type),
                record_type=event_type,
                payload=self.audit_payload(payload),
            )
        )
        await db.commit()
        result = {"seq": event.seq, "eventType": event.event_type, "data": event.event_json, "createdAt": datetime.now(timezone.utc).isoformat()}
        await self.broker.publish(conversation.conversation_id, result)
        if event_type == "assistant.message":
            getattr(self, "_answer_streams", {}).pop(conversation.conversation_id, None)
        return result

    async def publish_stream_event(self, conversation: Conversation, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Publish a live stream event without a remote database round trip.

        Token deltas are intentionally ephemeral. The completed assistant
        message and llm.response audit record are persisted after generation,
        so reconnects can recover the authoritative answer without committing
        once per model fragment.
        """
        conversation.last_seq += 1
        conversation.last_activity_at = datetime.now(timezone.utc)
        result = {
            "seq": conversation.last_seq,
            "eventType": event_type,
            "data": payload,
            "createdAt": datetime.now(timezone.utc).isoformat(),
        }
        await self.broker.publish(conversation.conversation_id, result)
        return result

    @staticmethod
    def status_phase_for_tool(tool_name: str) -> str:
        """Map an internal tool to a safe, customer-facing progress phase."""

        if tool_name == "ocr.layout_parsing":
            return "document"
        if tool_name == "knowledge.search":
            return "knowledge"
        if tool_name.startswith("umc."):
            return "umc"
        return "service"

    @staticmethod
    def status_message(language: str, phase: str) -> str:
        """Return a short progress message without exposing prompts or reasoning."""

        messages = {
            "en": {
                "routing": "I’m reviewing your request and selecting the right NMA service…",
                "knowledge": "I’m checking the relevant NMA guidance…",
                "document": "I’m analyzing the attached document…",
                "umc": "I’m checking your NMA service information…",
                "service": "I’m checking the requested NMA service…",
                "preparing": "I’m organizing the results into a clear answer…",
                "drafting": "I’m drafting your answer…",
                "fallback": "I’m preparing a response with the information currently available…",
            },
            "ar": {
                "routing": "أراجع طلبك وأحدد خدمة الهيئة الوطنية للإعلام المناسبة…",
                "knowledge": "أتحقق من إرشادات الهيئة الوطنية للإعلام ذات الصلة…",
                "document": "أحلل المستند المرفق…",
                "umc": "أتحقق من معلومات خدمة الهيئة الوطنية للإعلام الخاصة بك…",
                "service": "أتحقق من خدمة الهيئة المطلوبة…",
                "preparing": "أنظم النتائج في إجابة واضحة…",
                "drafting": "أصيغ إجابتك الآن…",
                "fallback": "أُعد إجابة بالمعلومات المتاحة حالياً…",
            },
        }
        language_messages = messages.get(language, messages["en"])
        return language_messages.get(phase, language_messages["preparing"])

    async def append_status(
        self,
        db: AsyncSession,
        conversation: Conversation,
        phase: str,
        language: str,
        *,
        request_id: str,
    ) -> None:
        """Publish a safe progress update; never include model reasoning or prompts."""

        await self.append_event(
            db,
            conversation,
            "assistant.status",
            {
                "phase": phase,
                "state": "running",
                "message": self.status_message(language, phase),
                "requestId": request_id,
                "runtimeId": conversation.runtime_id,
            },
        )

    @staticmethod
    def audit_category(record_type: str) -> str:
        if record_type.startswith("llm."):
            return "llm"
        if record_type in {"skill.route", "skill.route.shadow", "tool.call", "tool.result", "turn.started", "turn.completed", "runtime.error", "turn.cancelled"}:
            return "dsh"
        if record_type.startswith("user.") or record_type.startswith("assistant.") or record_type.startswith("message.feedback."):
            return "conversation"
        return "runtime"

    @classmethod
    def audit_payload(cls, value: Any, depth: int = 0) -> Any:
        """Redact credential-shaped fields while keeping content auditable."""

        if depth > 8:
            return "[max-depth]"
        if isinstance(value, dict):
            sensitive = {"token", "accesstoken", "umctoken", "authorization", "password", "apikey", "providerkey"}
            redacted: dict[str, Any] = {}
            for key, item in value.items():
                normalized_key = "".join(character for character in str(key).casefold() if character.isalnum())
                is_sensitive = (
                    normalized_key in sensitive
                    or normalized_key.endswith("token")
                    or "password" in normalized_key
                )
                redacted[str(key)] = "[redacted]" if is_sensitive else cls.audit_payload(item, depth + 1)
            return redacted
        if isinstance(value, list):
            return [cls.audit_payload(item, depth + 1) for item in value]
        return value

    async def append_audit(
        self,
        db: AsyncSession,
        conversation: Conversation,
        record_type: str,
        payload: dict[str, Any],
        *,
        request_id: str | None = None,
        runtime_id: str | None = None,
    ) -> None:
        db.add(
            AuditRecord(
                tenant_id=conversation.tenant_id,
                user_id=conversation.user_id,
                conversation_id=conversation.conversation_id,
                dsh_session_id=conversation.dsh_session_id,
                request_id=(request_id or str(payload.get("requestId") or ""))[:128] or None,
                runtime_id=(runtime_id or str(payload.get("runtimeId") or ""))[:128] or None,
                category=self.audit_category(record_type),
                record_type=record_type,
                payload=self.audit_payload(payload),
            )
        )
        await db.commit()

    async def purge_expired_audit(self) -> int:
        async with SessionLocal() as db:
            deleted = await purge_expired_audit_data(db, self.settings)
            await db.commit()
            return sum(deleted.values())

    async def submit_message(
        self,
        principal: Principal,
        conversation_id: str,
        content: str,
        client_message_id: str,
        attachment: dict[str, Any] | None = None,
        profile_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        async with self.writer_lock_for(conversation_id):
            async with SessionLocal() as db:
                conversation = await self.get_owned_conversation(db, principal, conversation_id)
                if principal.audit_account.strip():
                    conversation.owner_account = principal.audit_account.strip()[:300]
                existing = await db.execute(select(MessageIdempotency).where(MessageIdempotency.conversation_id == conversation_id, MessageIdempotency.client_message_id == client_message_id))
                idem = existing.scalar_one_or_none()
                if idem:
                    return {"accepted": False, "duplicate": True, "conversationId": conversation_id, "seq": idem.user_event_seq, "requestId": principal.request_id}
                active_task = self._turn_tasks.get(conversation_id)
                if active_task and not active_task.done():
                    return {"accepted": False, "duplicate": False, "busy": True, "code": "conversation_busy", "conversationId": conversation_id, "requestId": principal.request_id}
                lease = await self.runtime_manager.ensure_runtime(conversation_id, conversation.runtime_profile)
                conversation.runtime_id = lease.runtime_id
                conversation.status = "BUSY"
                event_payload: dict[str, Any] = {
                    "content": content,
                    "clientMessageId": client_message_id,
                    "requestId": principal.request_id,
                }
                audit_identity = {
                    "account": principal.audit_account,
                    "currentRole": principal.audit_current_role,
                }
                if audit_identity["account"] or audit_identity["currentRole"]:
                    event_payload["auditIdentity"] = audit_identity
                if attachment:
                    event_payload["attachment"] = attachment
                event = await self.append_event(db, conversation, "user.message", event_payload)
                db.add(MessageIdempotency(conversation_id=conversation_id, client_message_id=client_message_id, user_event_seq=event["seq"]))
                await db.commit()
                await self.runtime_manager.mark_busy(conversation_id)
                self._turn_tasks[conversation_id] = asyncio.create_task(
                    self._run_turn(
                        principal,
                        conversation_id,
                        profile_context_from_payload(profile_context, trusted_profile_id=principal.profile_id),
                    )
                )
                return {"accepted": True, "duplicate": False, "conversationId": conversation_id, "seq": event["seq"], "requestId": principal.request_id, "runtimeId": lease.runtime_id}

    async def _run_turn(self, principal: Principal, conversation_id: str, profile_context: ProfileContext | None = None) -> None:
        if not hasattr(self, "_answer_streams"):
            self._answer_streams = {}
        async with self.writer_lock_for(conversation_id):
            try:
                async with SessionLocal() as db:
                    conversation = await self.get_owned_conversation(db, principal, conversation_id)
                    history = await self.list_events(db, conversation, after_seq=0)
                    messages = [{"role": "user" if event.event_type == "user.message" else "assistant", "content": event.event_json.get("content", "")} for event in history if event.event_type in {"user.message", "assistant.message"}]
                    await self.append_event(db, conversation, "turn.started", {"requestId": principal.request_id, "runtimeId": conversation.runtime_id})
                    latest_user = next((event for event in reversed(history) if event.event_type == "user.message"), None)
                    latest_content = latest_user.event_json.get("content", "") if latest_user else ""
                    raw_attachment = latest_user.event_json.get("attachment") if latest_user else None
                    latest_attachment = raw_attachment if isinstance(raw_attachment, dict) else None
                    response_language = response_language_for(latest_content)

                    async def publish_block(payload: dict) -> dict:
                        return await self.publish_stream_event(conversation, "assistant.chunk", {
                            **payload, "requestId": principal.request_id,
                            "runtimeId": conversation.runtime_id,
                        })
                    answer_stream = AnswerStream(publish_block)
                    self._answer_streams[conversation_id] = (answer_stream, response_language)
                    # Send a first visible update before deterministic routing,
                    # external calls, or the LLM request can spend time waiting.
                    await self.append_status(
                        db,
                        conversation,
                        "routing",
                        response_language,
                        request_id=principal.request_id,
                    )
                    scope_guard = self.profile_scope_guard(latest_content, response_language, profile_context)
                    if scope_guard:
                        await self.append_audit(
                            db,
                            conversation,
                            "profile.scope.guard",
                            {"requestId": principal.request_id, "runtimeId": conversation.runtime_id, "code": scope_guard["code"]},
                            request_id=principal.request_id,
                            runtime_id=conversation.runtime_id,
                        )
                        await self.append_event(
                            db,
                            conversation,
                            "assistant.message",
                            {
                                "content": scope_guard["content"],
                                "profileAction": scope_guard.get("profileAction"),
                                "requestId": principal.request_id,
                            },
                        )
                        await self.append_event(
                            db,
                            conversation,
                            "turn.completed",
                            {"requestId": principal.request_id, "runtimeId": conversation.runtime_id},
                        )
                        conversation.status = "READY"
                        conversation.last_activity_at = datetime.now(timezone.utc)
                        await db.commit()
                        lease = self.runtime_manager.get(conversation_id)
                        if lease:
                            lease.state = "READY"
                        return
                    if is_public_service_fee_query(latest_content) and not latest_attachment:
                        # Read the same live Learn More source as Customer Portal.
                        # No model, knowledge fee schedule, or caller identity
                        # selector may supply or replace the published fee.
                        await self.append_event(db, conversation, "skill.route", {
                            "skillId": "service_fees", "category": "data_query",
                            "toolName": "umc.services.fees", "mode": "answer",
                            "requestId": principal.request_id,
                        })
                        await self.append_event(db, conversation, "tool.call", {
                            "toolName": "umc.services.fees", "arguments": {"query": latest_content[:500]},
                            "requestId": principal.request_id,
                        })
                        try:
                            if not self.settings.external_tools_enabled:
                                raise RuntimeError("External tools are disabled")
                            fee_result = await CustomerServiceFeeClient(self.settings).lookup(latest_content, umc_token=principal.umc_token)
                            fee_content = fee_answer(fee_result, response_language)
                            fee_event = {"ok": True, "result": fee_result.model_dump()}
                        except (httpx.HTTPError, PermissionError, RuntimeError, ValueError):
                            fee_content = (
                                "تعذر قراءة رسوم بطاقة الخدمة حالياً. راجع اعرف المزيد في صفحة الخدمات أو حاول مرة أخرى. لا يمكنني تقدير الرسوم."
                                if response_language == "ar" else
                                "I could not read the service card fee right now. Check Learn More on the Services page or retry. I cannot estimate the fee."
                            )
                            fee_event = {"ok": False, "code": "service_fee_unavailable"}
                        await self.append_event(db, conversation, "tool.result", {
                            "toolName": "umc.services.fees", **fee_event, "requestId": principal.request_id,
                        })
                        await self.append_event(db, conversation, "assistant.message", {
                            "content": fee_content, "requestId": principal.request_id,
                        })
                        await self.append_event(db, conversation, "turn.completed", {
                            "requestId": principal.request_id, "runtimeId": conversation.runtime_id,
                        })
                        conversation.status = "READY"
                        conversation.last_activity_at = datetime.now(timezone.utc)
                        await db.commit()
                        lease = self.runtime_manager.get(conversation_id)
                        if lease:
                            lease.state = "READY"
                        return
                    # Published Skill workflow is authoritative for deterministic
                    # routing. Built-in definitions are only a cold-start fallback.
                    route_catalog = await self.skill_catalog.load(db)
                    keyword_route = (
                        resolve_configured_skill(latest_content, route_catalog, canonicalize=False)
                        or resolve_skill(latest_content)
                    )
                    if self.is_application_list_request(latest_content):
                        application_status_entry = next(
                            (item for item in route_catalog if str(item.get("skillId") or "") == "application_status"),
                            None,
                        )
                        if application_status_entry:
                            # Keep application list lookups on the published,
                            # read-only UMC workflow even when the sentence
                            # also names a Profile.
                            keyword_route = self.route_shape_for_skill(
                                "application_status", route_catalog, routing_locked=True
                            )
                    route_context = route_context_from_history(history, route_catalog)
                    # The current question is sent separately to the router;
                    # keep only preceding turns in the auxiliary context.
                    if route_context.get("recentMessages") and route_context["recentMessages"][-1].get("role") == "user" and route_context["recentMessages"][-1].get("content") == latest_content:
                        route_context["recentMessages"] = route_context["recentMessages"][:-1]
                    active_skill_id = str(route_context.get("activeSkillId") or "")
                    cross_skill_handoff: dict[str, Any] | None = None
                    if active_skill_id:
                        active_catalog_entry = next(
                            (item for item in route_catalog if str(item.get("skillId") or "") == active_skill_id),
                            None,
                        ) or next(
                            (
                                item
                                for item in route_catalog
                                if canonical_skill_id(str(item.get("skillId") or "")) == active_skill_id
                            ),
                            None,
                        )
                        active_skill_record_id = str((active_catalog_entry or {}).get("skillId") or active_skill_id)
                        active_skill_result = await db.execute(
                            select(Skill)
                            .where(
                                Skill.skill_id == active_skill_record_id,
                                Skill.scope == "system",
                                Skill.enabled.is_(True),
                                Skill.status == "PUBLISHED",
                            )
                            .order_by(Skill.version.desc())
                        )
                        active_skill = active_skill_result.scalars().first()
                        if active_skill:
                            cross_skill_handoff = self.configured_cross_skill_handoff(
                                active_skill, latest_content, history, route_catalog
                            )
                        if cross_skill_handoff:
                            target_skill_id = str(cross_skill_handoff.get("targetSkillId") or "")
                            target_entry = next(
                                (item for item in route_catalog if str(item.get("skillId") or "") == target_skill_id),
                                None,
                            )
                            if target_entry:
                                keyword_route = self.route_shape_for_skill(
                                    target_skill_id, route_catalog, routing_locked=True
                                )
                            else:
                                cross_skill_handoff = None
                        elif active_skill and matches_configured_selection_follow_up(
                            merged_skill_workflow(active_skill.skill_id, active_skill.workflow), latest_content, history
                        ):
                            keyword_route = self.route_shape_for_skill(
                                active_skill_record_id,
                                route_catalog,
                                routing_locked=True,
                            )
                    attachment_ocr_route = self.attachment_ocr_route(latest_attachment)
                    if attachment_ocr_route:
                        # A newly uploaded document is always analyzed before
                        # cross-Skill follow-ups, LLM selection, or knowledge
                        # fallback.  OCR failures receive a controlled error
                        # below rather than being silently rerouted.
                        keyword_route = attachment_ocr_route
                        cross_skill_handoff = None
                    route, route_metadata = await self.choose_skill_route(
                        db, latest_content, keyword_route, conversation, principal.request_id, route_context
                    )
                    if attachment_ocr_route:
                        route_metadata["attachmentOcrForced"] = True
                    if cross_skill_handoff:
                        route_metadata.update(
                            {
                                "crossSkillHandoffId": cross_skill_handoff.get("id"),
                                "crossSkillSourceSkillId": cross_skill_handoff.get("sourceSkillId"),
                                "crossSkillSourceToolName": cross_skill_handoff.get("sourceToolName"),
                                "crossSkillHandoffMissing": bool(cross_skill_handoff.get("missing")),
                            }
                        )
                    selected_skill_result = await db.execute(
                        select(Skill)
                        .where(
                            Skill.skill_id == route.skill_id,
                            Skill.scope == "system",
                            Skill.enabled.is_(True),
                            Skill.status == "PUBLISHED",
                        )
                        .order_by(Skill.version.desc())
                    )
                    selected_skill = selected_skill_result.scalars().first()
                    profile_sensitive_request = (
                        route.skill_id == "profile_status"
                        and self.is_profile_sensitive_data_request(latest_content)
                    )
                    system_tool_definitions = system_default_tool_definitions(self.settings)
                    system_tool_map = {item["toolName"]: item for item in system_tool_definitions}
                    configured_system_tools = {name for name, item in system_tool_map.items() if item.get("enabled") and item.get("published")}
                    allowed_tool_names = [
                        name for name in (list(selected_skill.allowed_tools) if selected_skill else [])
                        if name not in SYSTEM_DEFAULT_TOOL_NAMES or name in configured_system_tools
                    ]
                    handoff_tool_request = cross_skill_handoff.get("toolRequest") if cross_skill_handoff else None
                    handoff_missing_message = (
                        str(cross_skill_handoff.get("missingMessage") or "")
                        if cross_skill_handoff and cross_skill_handoff.get("missing")
                        else ""
                    )
                    if attachment_ocr_route:
                        tool_request = (
                            (
                                "ocr.layout_parsing",
                                {
                                    "attachment": latest_attachment,
                                    "fileType": latest_attachment.get("fileType"),
                                },
                            )
                            if "ocr.layout_parsing" in allowed_tool_names
                            else None
                        )
                    else:
                        tool_request = parse_tool_request(latest_content) if latest_user else None
                    if handoff_tool_request and not latest_attachment:
                        tool_request = handoff_tool_request
                    if route.mode == "portal_action":
                        # Downloads, exports, payments, and refunds are visible
                        # Portal actions only for this read-only release.
                        tool_request = None
                    elif not tool_request and route.category == "knowledge" and route.mode == "exact_quote" and not exact_quote_source_sufficient(latest_content):
                        # Do not retrieve and let ranking choose a random law for
                        # an unqualified exact-quotation request.
                        tool_request = None
                    elif not tool_request and route.category == "knowledge" and self.settings.knowledge_default_folder_id:
                        tool_request = (
                            "knowledge.search",
                            {
                                "query": build_knowledge_query(route, latest_content),
                                "folder_id": self.settings.knowledge_default_folder_id,
                                "top_k": self.settings.knowledge_top_k,
                            },
                        )
                    elif not tool_request and not handoff_missing_message:
                        tool_request = build_configured_tool_request(
                            merged_skill_workflow(selected_skill.skill_id, selected_skill.workflow) if selected_skill else {},
                            allowed_tool_names,
                            latest_content,
                            history,
                            intent_id=route_metadata.get("intentId"),
                            filters=route_metadata.get("filters"),
                        )
                        if not tool_request:
                            # Legacy request parsing remains for established
                            # Skills that have not yet defined a workflow.
                            tool_request = build_legacy_tool_request(
                                allowed_tool_names,
                                latest_content,
                                mode=route.mode,
                                skill_id=route.skill_id,
                            )
                    elif tool_request and tool_request[0] == "knowledge.search":
                        tool_name, arguments = tool_request
                        arguments = dict(arguments)
                        arguments["query"] = build_knowledge_query(route, str(arguments.get("query", latest_content)))
                        if not arguments.get("folder_id") and self.settings.knowledge_default_folder_id:
                            arguments["folder_id"] = self.settings.knowledge_default_folder_id
                        if "top_k" not in arguments:
                            arguments["top_k"] = self.settings.knowledge_top_k
                        tool_request = (tool_name, arguments)
                    if profile_sensitive_request:
                        # Do not expose Profile data to a drafting model for a
                        # request whose only purpose is revealing an identifier.
                        tool_request = None
                    await self.append_event(
                        db,
                        conversation,
                        "skill.route",
                        {
                            "skillId": route.skill_id,
                            "category": route.category,
                            "toolName": None if profile_sensitive_request else route.tool_name or (tool_request[0] if tool_request else None),
                            "mode": route.mode,
                            "fields": list(route.fields),
                            "requestId": principal.request_id,
                            "routerMode": route_metadata.get("routerMode"),
                            "keywordSkillId": route_metadata.get("keywordSkillId"),
                            "llmSkillId": route_metadata.get("llmSkillId"),
                            "intentId": route_metadata.get("intentId"),
                            "filters": route_metadata.get("filters"),
                            "routeConsistent": route_metadata.get("routeConsistent"),
                            "fallbackReason": route_metadata.get("fallbackReason"),
                            "fallbackSkillId": route_metadata.get("fallbackSkillId"),
                            "candidateSkillIds": route_metadata.get("candidateSkillIds"),
                            "candidateDomainIds": route_metadata.get("candidateDomainIds"),
                            "domainScores": route_metadata.get("domainScores"),
                            "routingLocked": route_metadata.get("routingLocked", False),
                            "attachmentOcrForced": route_metadata.get("attachmentOcrForced", False),
                            "routeContextUsed": route_metadata.get("routeContextUsed"),
                            "confidence": route_metadata.get("confidence"),
                            "needsClarification": route_metadata.get("needsClarification"),
                            "crossSkillHandoffId": route_metadata.get("crossSkillHandoffId"),
                            "crossSkillSourceSkillId": route_metadata.get("crossSkillSourceSkillId"),
                            "crossSkillSourceToolName": route_metadata.get("crossSkillSourceToolName"),
                            "crossSkillHandoffMissing": route_metadata.get("crossSkillHandoffMissing", False),
                        },
                    )
                    if tool_request:
                        await self.append_status(
                            db,
                            conversation,
                            self.status_phase_for_tool(tool_request[0]),
                            response_language,
                            request_id=principal.request_id,
                        )
                    else:
                        await self.append_status(
                            db,
                            conversation,
                            "preparing",
                            response_language,
                            request_id=principal.request_id,
                        )
                    selected_tool_docs: list[dict[str, Any]] = [
                        {
                            "name": item["toolName"],
                            "description": item["description"],
                            "parameters": item["parameters"],
                            "sideEffect": item["sideEffect"],
                            "confirmationRequired": item["confirmationRequired"],
                        }
                        for name, item in system_tool_map.items()
                        if name in allowed_tool_names
                    ]
                    tool_definition_by_name: dict[str, dict[str, Any]] = {
                        name: system_tool_map[name]
                        for name in allowed_tool_names
                        if name in system_tool_map
                    }
                    if selected_skill and allowed_tool_names:
                        selected_tools_result = await db.execute(
                            select(Tool).where(
                                ~Tool.tool_name.in_(SYSTEM_DEFAULT_TOOL_NAMES),
                                Tool.tool_name.in_(allowed_tool_names),
                                Tool.enabled.is_(True),
                                Tool.published.is_(True),
                            )
                        )
                        selected_tool_docs = [
                            {
                                "name": item.tool_name,
                                "description": item.description,
                                "parameters": item.parameters,
                                "sideEffect": item.side_effect,
                                "confirmationRequired": item.confirmation_required,
                                "operationId": item.operation_id,
                                "httpMethod": item.http_method,
                                "httpPath": item.http_path,
                                "authStrategy": item.auth_strategy,
                                "rbacPolicy": item.rbac_policy,
                                "maskingPolicy": item.masking_policy,
                                "profileScope": item.profile_scope,
                                "source": item.source,
                            }
                            for item in selected_tools_result.scalars().all()
                        ]
                        tool_definition_by_name.update({item["name"]: item for item in selected_tool_docs})
                    messages.insert(
                        0,
                        {
                            "role": "system",
                            "content": build_system_prompt(
                                route,
                                evidence_available=bool(tool_request),
                                response_language=response_language,
                                operator_prompt=str(getattr(self.settings, "system_prompt", "") or ""),
                                skill_content=selected_skill.content if selected_skill else "",
                                profile_context=profile_context,
                            ),
                        },
                    )
                    if route.category in {"data_query", "api_call"}:
                        messages.insert(1, {"role": "system", "content": "FLOW INTERACTION CONSTRAINTS: " + json.dumps(build_flow_prompt(route), ensure_ascii=False)})
                    forced_response_message: str | None = handoff_missing_message or (
                        self.profile_sensitive_data_refusal(response_language)
                        if profile_sensitive_request
                        else "تعذر علي تحليل الملف المرفق لأن إعداد تحليل المستندات غير متاح حالياً. "
                        "يرجى المحاولة مرة أخرى بعد توفر خدمة OCR."
                        if attachment_ocr_route and "ocr.layout_parsing" not in allowed_tool_names and response_language == "ar"
                        else "I could not analyze the attached file because document analysis is not configured right now. Please try again after the OCR service is available."
                        if attachment_ocr_route and "ocr.layout_parsing" not in allowed_tool_names
                        else None
                    )
                    attachment_local_response: str | None = None
                    attachment_handoff_skill_id: str | None = None
                    attachment_handoff_result: dict[str, Any] | None = None
                    if tool_request:
                        tool_name, arguments = tool_request
                        if tool_name == "umc.applications":
                            arguments = application_page_arguments(arguments)
                        tool_definition = tool_definition_by_name.get(tool_name) or {}
                        target_profile = requires_profile_switch(
                            tool_definition,
                            profile_context,
                            latest_content,
                        )
                        requires_selection = (
                            profile_scope_for_definition(tool_definition).get("mode") == "bind_parameter"
                            and (not profile_context or profile_context.is_global_view)
                        )
                        if target_profile or requires_selection:
                            switch_message = (
                                f"يرجى التبديل إلى ملف {target_profile.name} في البوابة أولاً، ثم أرسل الطلب مرة أخرى."
                                if target_profile and response_language == "ar"
                                else "يرجى اختيار ملف شخصي في البوابة أولاً، ثم أرسل الطلب مرة أخرى."
                                if response_language == "ar"
                                else f"Please switch to the {target_profile.name} profile in the portal, then ask again."
                                if target_profile
                                else "Please select a profile in the portal, then ask again."
                            )
                            await self.append_event(db, conversation, "profile.scope", {"outcome": "switch_required" if target_profile else "selection_required", "targetProfileId": target_profile.profile_id if target_profile else None, "requestId": principal.request_id})
                            profile_action: dict[str, Any] = {
                                "type": "open_profile_menu",
                                "code": "profile_switch_required" if target_profile else "profile_selection_required",
                            }
                            if target_profile:
                                profile_action["targetProfile"] = {
                                    "profileId": target_profile.profile_id,
                                    "profileName": target_profile.name,
                                }
                            await self.append_event(
                                db,
                                conversation,
                                "assistant.message",
                                {
                                    "content": switch_message,
                                    "profileAction": profile_action,
                                    "requestId": principal.request_id,
                                },
                            )
                            await self.append_event(db, conversation, "turn.completed", {"requestId": principal.request_id, "runtimeId": conversation.runtime_id})
                            conversation.status = "READY"
                            conversation.last_activity_at = datetime.now(timezone.utc)
                            await db.commit()
                            lease = self.runtime_manager.get(conversation_id)
                            if lease:
                                lease.state = "READY"
                            return
                        attachment_argument = arguments.get("attachment")
                        parameter_schema = (tool_definition_by_name.get(tool_name) or {}).get("parameters") or {}
                        declared_parameters = parameter_schema.get("properties", {}) if isinstance(parameter_schema, dict) else {}
                        audited_parameters = {
                            key: self.audit_payload(value)
                            for key, value in arguments.items()
                            if key in declared_parameters and key not in {"file", "attachment"}
                        }
                        safe_arguments = {
                            "fileType": arguments.get("fileType"),
                            "hasFile": bool(arguments.get("file") or attachment_argument),
                            "attachment": {
                                "fileName": attachment_argument.get("fileName"),
                                "mimeType": attachment_argument.get("mimeType"),
                                "fileType": attachment_argument.get("fileType"),
                            } if isinstance(attachment_argument, dict) else None,
                            "query": str(arguments.get("query", ""))[:500] if "query" in arguments else None,
                            "folderId": arguments.get("folder_id") or arguments.get("folderId"),
                            "topK": arguments.get("top_k") or arguments.get("topK") or 32,
                            "pageIndex": arguments.get("page_index") or arguments.get("pageIndex"),
                            "pageSize": arguments.get("page_size") or arguments.get("pageSize"),
                            "applicationId": arguments.get("applicationId") or arguments.get("application_id"),
                            "isbn": str(arguments.get("isbn", ""))[:32] if "isbn" in arguments else None,
                            "parameterKeys": sorted(arguments.get("parameters", {}).keys()) if isinstance(arguments.get("parameters"), dict) else None,
                            "parameters": audited_parameters,
                        }
                        await self.append_event(db, conversation, "tool.call", {"toolName": tool_name, "arguments": safe_arguments, "requestId": principal.request_id})
                        if not self.settings.external_tools_enabled:
                            tool_result = {"ok": False, "code": "external_tools_disabled", "toolName": tool_name}
                        elif isinstance(attachment_argument, dict):
                            try:
                                document_base64 = await self.documents.as_base64(
                                    str(attachment_argument.get("fileRef", "")),
                                    mime_type=str(attachment_argument.get("mimeType", "")),
                                    umc_token=principal.umc_token,
                                )
                                tool_result = await self.tool_gateway.invoke(
                                    principal,
                                    tool_name,
                                    {
                                        "file": document_base64,
                                        "fileType": attachment_argument.get("fileType"),
                                    },
                                    allowed_tools=allowed_tool_names,
                                    tool_definition=tool_definition_by_name.get(tool_name),
                                    profile_context=profile_context,
                                )
                            except (httpx.HTTPError, PermissionError, RuntimeError, ValueError) as exc:
                                tool_result = {
                                    "ok": False,
                                    "code": "tool_unavailable",
                                    "toolName": tool_name,
                                    "error": str(exc)[:500],
                                }
                        else:
                            tool_result = await self.tool_gateway.invoke(
                                principal,
                                tool_name,
                                arguments,
                                allowed_tools=allowed_tool_names,
                                tool_definition=tool_definition_by_name.get(tool_name),
                                profile_context=profile_context,
                            )
                        masking_policy = str((tool_definition_by_name.get(tool_name) or {}).get("maskingPolicy") or "default")
                        masked_tool_result = mask_tool_result(tool_result, masking_policy)
                        if tool_name == "umc.applications" and not latest_attachment and not forced_response_message:
                            forced_response_message = application_list_answer(latest_content, masked_tool_result, response_language, profile_context)
                        # OCR text is sensitive customer data.  Keep the OCR
                        # output inside this process and persist only bounded
                        # reference hints for the audit trail.
                        result_for_event = (
                            self.attachment_ocr_event_result(tool_result)
                            if latest_attachment and tool_name == "ocr.layout_parsing"
                            else dict(masked_tool_result)
                        )
                        if not (latest_attachment and tool_name == "ocr.layout_parsing") and isinstance(result_for_event.get("result"), dict):
                            result_for_event["result"] = json.dumps(result_for_event["result"], ensure_ascii=False)[:20_000]
                        await self.append_event(db, conversation, "tool.result", result_for_event)
                        resolved_document_names: str | None = None
                        document_name_tool = "umc.documents.original_names"
                        if (
                            tool_result.get("ok")
                            and tool_name == "umc.violations.detail"
                            and document_name_tool in allowed_tool_names
                            and document_name_tool in tool_definition_by_name
                        ):
                            evidence_keys = self.violation_evidence_keys(tool_result)
                            if evidence_keys:
                                document_name_definition = tool_definition_by_name[document_name_tool]
                                await self.append_event(
                                    db,
                                    conversation,
                                    "tool.call",
                                    {
                                        "toolName": document_name_tool,
                                        "arguments": {"parameterKeys": ["keys"], "keyCount": len(evidence_keys)},
                                        "requestId": principal.request_id,
                                    },
                                )
                                document_name_result = await self.tool_gateway.invoke(
                                    principal,
                                    document_name_tool,
                                    {"keys": evidence_keys},
                                    allowed_tools=allowed_tool_names,
                                    tool_definition=document_name_definition,
                                    profile_context=profile_context,
                                )
                                document_name_masking = str(document_name_definition.get("maskingPolicy") or "default")
                                document_name_event = mask_tool_result(document_name_result, document_name_masking)
                                if isinstance(document_name_event.get("result"), dict):
                                    document_name_event["result"] = json.dumps(
                                        document_name_event["result"], ensure_ascii=False
                                    )[:20_000]
                                await self.append_event(db, conversation, "tool.result", document_name_event)
                                if document_name_result.get("ok"):
                                    resolved_document_names = self.original_document_name_evidence(document_name_result)
                        if not latest_attachment:
                            related_document_instruction = (
                                "\nTRUSTED DOCUMENT NAME RESULT: The customer-visible evidence filename(s) are "
                                + resolved_document_names
                                + ". Storage keys from the violation-detail result are internal references, not filenames."
                                if resolved_document_names
                                else ""
                            )
                            messages.insert(
                                2,
                                {
                                    "role": "system",
                                    "content": (
                                        "TRUSTED TOOL RESULT: The lookup below has already completed. "
                                        "Use these returned values as the authoritative source for the answer. "
                                        "Do not ask the user to confirm a profile, category, or record that is already present, "
                                        "and never claim there are no records when this result contains items.\n"
                                        "currentPortalProfile is the current portal identity label; use it to explain the selected data scope, even when the records omit a Profile name column. Do not request another switch to that same Profile. evidenceTruncated means this is a bounded excerpt, not missing source records.\n"
                                        + self.answer_tool_evidence(tool_name, tool_result, masking_policy, profile_context)
                                        + related_document_instruction
                                    ),
                                },
                            )
                        await self.append_status(
                            db,
                            conversation,
                            "fallback" if not tool_result.get("ok") else "preparing",
                            response_language,
                            request_id=principal.request_id,
                        )
                        if latest_attachment and tool_result.get("ok") and latest_content.strip():
                            # Privacy-preserving second stage: the business
                            # intent must be explicit in the user's text.  OCR
                            # output contributes only locally extracted
                            # references to Tool argument matching; it is never
                            # sent to the external LLM router.
                            handoff_text = self.attachment_ocr_handoff_text(latest_content, tool_result)
                            handoff_skill_id = self.attachment_ocr_explicit_skill(
                                latest_content, route_catalog
                            )
                            excluded_handoff_skills = {
                                "document_ocr",
                                "general",
                                "general_knowledge",
                            }
                            if (
                                handoff_skill_id
                                and handoff_skill_id not in excluded_handoff_skills
                            ):
                                handoff_route = self.route_shape_for_skill(
                                    handoff_skill_id, route_catalog, routing_locked=True
                                )
                                handoff_skill_result = await db.execute(
                                    select(Skill)
                                    .where(
                                        Skill.skill_id == handoff_route.skill_id,
                                        Skill.scope == "system",
                                        Skill.enabled.is_(True),
                                        Skill.status == "PUBLISHED",
                                    )
                                    .order_by(Skill.version.desc())
                                )
                                handoff_skill = handoff_skill_result.scalars().first()
                                handoff_allowed = list(handoff_skill.allowed_tools) if handoff_skill else []
                                handoff_definitions = {
                                    name: item for name, item in system_tool_map.items() if name in handoff_allowed
                                }
                                if handoff_allowed:
                                    handoff_tools_result = await db.execute(
                                        select(Tool).where(
                                            Tool.tool_name.in_(handoff_allowed),
                                            Tool.enabled.is_(True),
                                            Tool.published.is_(True),
                                        )
                                    )
                                    handoff_definitions.update(
                                        {
                                            item.tool_name: {
                                                "name": item.tool_name,
                                                "parameters": item.parameters,
                                                "sideEffect": item.side_effect,
                                                "confirmationRequired": item.confirmation_required,
                                                "maskingPolicy": item.masking_policy,
                                                "profileScope": item.profile_scope,
                                                "source": item.source,
                                            }
                                            for item in handoff_tools_result.scalars().all()
                                        }
                                    )
                                handoff_allowed = [
                                    name
                                    for name in handoff_allowed
                                    if self.is_read_only_tool_definition(handoff_definitions.get(name))
                                ]
                                handoff_request = build_configured_tool_request(
                                    merged_skill_workflow(handoff_skill.skill_id, handoff_skill.workflow)
                                    if handoff_skill
                                    else {},
                                    handoff_allowed,
                                    handoff_text,
                                    history,
                                )
                                if not handoff_request:
                                    handoff_request = build_legacy_tool_request(
                                        handoff_allowed,
                                        handoff_text,
                                        mode=handoff_route.mode,
                                        skill_id=handoff_route.skill_id,
                                    )
                                if (
                                    handoff_request
                                    and handoff_request[0] != "knowledge.search"
                                    and handoff_request[0] in handoff_allowed
                                ):
                                    handoff_tool_name, handoff_arguments = handoff_request
                                    handoff_definition = handoff_definitions[handoff_tool_name]
                                    target_profile = requires_profile_switch(
                                        handoff_definition, profile_context, latest_content
                                    )
                                    requires_selection = (
                                        profile_scope_for_definition(handoff_definition).get("mode")
                                        == "bind_parameter"
                                        and (not profile_context or profile_context.is_global_view)
                                    )
                                    if not target_profile and not requires_selection:
                                        await self.append_event(
                                            db,
                                            conversation,
                                            "skill.route",
                                            {
                                                "skillId": handoff_route.skill_id,
                                                "category": handoff_route.category,
                                                "toolName": handoff_tool_name,
                                                "mode": handoff_route.mode,
                                                "requestId": principal.request_id,
                                                "routingLocked": True,
                                                "attachmentOcrHandoff": True,
                                            },
                                        )
                                        await self.append_event(
                                            db,
                                            conversation,
                                            "tool.call",
                                            {
                                                "toolName": handoff_tool_name,
                                                "arguments": {
                                                    "attachmentOcrHandoff": True,
                                                    "parameterKeys": sorted(handoff_arguments),
                                                },
                                                "requestId": principal.request_id,
                                            },
                                        )
                                        handoff_result = await self.tool_gateway.invoke(
                                            principal,
                                            handoff_tool_name,
                                            handoff_arguments,
                                            allowed_tools=handoff_allowed,
                                            tool_definition=handoff_definition,
                                            profile_context=profile_context,
                                        )
                                        handoff_masked_result = mask_tool_result(
                                            handoff_result,
                                            str(handoff_definition.get("maskingPolicy") or "default"),
                                        )
                                        handoff_event_result = dict(handoff_masked_result)
                                        if isinstance(handoff_event_result.get("result"), dict):
                                            handoff_event_result["result"] = json.dumps(
                                                handoff_event_result["result"], ensure_ascii=False
                                            )[:20_000]
                                        await self.append_event(
                                            db, conversation, "tool.result", handoff_event_result
                                        )
                                        attachment_handoff_skill_id = handoff_route.skill_id
                                        attachment_handoff_result = handoff_result
                                        route = handoff_route
                                        selected_skill = handoff_skill
                        if latest_attachment and not tool_result.get("ok"):
                            forced_response_message = (
                                "تعذر علي قراءة الملف المرفق لأن خدمة تحليل المستندات غير متاحة حالياً. "
                                "يرجى المحاولة مرة أخرى بعد توفر خدمة OCR."
                                if response_language == "ar"
                                else "I could not read the attached file because document analysis is unavailable right now. Please try again after the OCR service is available."
                            )
                        elif latest_attachment:
                            # Attachment turns are completed by the local OCR
                            # and deterministic read-only formatter.  Neither
                            # OCR text nor the resulting business evidence is
                            # passed to the external answer LLM.
                            attachment_local_response = self.attachment_ocr_local_response(
                                response_language=response_language,
                                ocr_result=tool_result,
                                handoff_skill_id=attachment_handoff_skill_id,
                                handoff_result=attachment_handoff_result,
                            )
                    if forced_response_message:
                        await self.append_event(db, conversation, "assistant.message", {"content": forced_response_message, "requestId": principal.request_id})
                    elif attachment_local_response:
                        await self.append_audit(
                            db,
                            conversation,
                            "ocr.local_response",
                            {
                                "requestId": principal.request_id,
                                "runtimeId": conversation.runtime_id,
                                "attachmentOcrExternalLlm": False,
                                "handoffSkillId": attachment_handoff_skill_id,
                                "referenceHints": self.attachment_ocr_reference_hints(tool_result),
                            },
                            request_id=principal.request_id,
                            runtime_id=conversation.runtime_id,
                        )
                        await answer_stream.verified(attachment_local_response)
                        await self.append_event(
                            db,
                            conversation,
                            "assistant.message",
                            {"content": attachment_local_response, "requestId": principal.request_id},
                        )
                    else:
                        await self.append_status(
                            db,
                            conversation,
                            "drafting",
                            response_language,
                            request_id=principal.request_id,
                        )
                        llm_started = time.perf_counter()
                        await self.append_audit(
                            db,
                            conversation,
                            "llm.request",
                            {
                                "requestId": principal.request_id,
                                "runtimeId": conversation.runtime_id,
                                "model": self.settings.llm_model,
                                "baseUrl": self.settings.llm_base_url,
                                "stream": True,
                                "messages": messages,
                            },
                            request_id=principal.request_id,
                            runtime_id=conversation.runtime_id,
                        )
                        try:
                            sanitizer = getattr(self, "sanitize_customer_answer", None)
                            content, reasoning, stream_status = await customer_generated_answer(
                                answer_stream, self.llm, messages, response_language,
                                sanitize=(lambda text: sanitizer(text, response_language)) if sanitizer else (lambda text: text),
                            )
                            await self.append_audit(db, conversation, "answer.stream", {
                                "streamVersion": answer_stream.protocol_version,
                                "blockCount": answer_stream.block_count, "status": stream_status,
                                "blockReceipts": answer_stream.block_receipts,
                                "requestId": principal.request_id, "runtimeId": conversation.runtime_id,
                            }, request_id=principal.request_id, runtime_id=conversation.runtime_id)
                        except Exception as exc:
                            await self.append_audit(
                                db,
                                conversation,
                                "llm.error",
                                {
                                    "requestId": principal.request_id,
                                    "runtimeId": conversation.runtime_id,
                                    "error": str(exc)[:500],
                                    "durationMs": round((time.perf_counter() - llm_started) * 1000, 1),
                                },
                                request_id=principal.request_id,
                                runtime_id=conversation.runtime_id,
                            )
                            raise
                        await self.append_audit(
                            db,
                            conversation,
                            "llm.response",
                            {
                                "requestId": principal.request_id,
                                "runtimeId": conversation.runtime_id,
                                "model": self.settings.llm_model,
                                "content": content,
                                "reasoning": reasoning,
                                "durationMs": round((time.perf_counter() - llm_started) * 1000, 1),
                            },
                            request_id=principal.request_id,
                            runtime_id=conversation.runtime_id,
                        )
                        if reasoning:
                            await self.append_audit(
                                db,
                                conversation,
                                "llm.thought",
                                {"requestId": principal.request_id, "runtimeId": conversation.runtime_id, "content": reasoning},
                                request_id=principal.request_id,
                                runtime_id=conversation.runtime_id,
                            )
                        await self.append_event(db, conversation, "assistant.message", {"content": content, "requestId": principal.request_id})
                    await self.append_event(db, conversation, "turn.completed", {"requestId": principal.request_id, "runtimeId": conversation.runtime_id})
                    conversation.status = "READY"
                    conversation.last_activity_at = datetime.now(timezone.utc)
                    await db.commit()
                lease = self.runtime_manager.get(conversation_id)
                if lease:
                    lease.state = "READY"
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                active = self._answer_streams.pop(conversation_id, None)
                async with SessionLocal() as db:
                    try:
                        conversation = await self.get_owned_conversation(db, principal, conversation_id)
                        if active and active[0].content:
                            stream, language = active
                            conversation.last_seq = max(conversation.last_seq, stream.last_seq)
                            await stream.verified(stream.content + incomplete_stream_notice(language))
                            conversation.last_seq = max(conversation.last_seq, stream.last_seq)
                            await self.append_event(db, conversation, "assistant.message", {
                                "content": stream.content, "streamVersion": stream.protocol_version,
                                "streamStatus": "partial", "requestId": principal.request_id,
                            })
                        conversation.status = "DEAD"
                        conversation.last_error = str(exc)[:1_000]
                        await self.append_event(db, conversation, "runtime.error", {"requestId": principal.request_id, "error": str(exc)[:500]})
                        await db.commit()
                    except Exception:
                        pass

    async def cancel(self, principal: Principal, conversation_id: str) -> None:
        async with SessionLocal() as db:
            await self.get_owned_conversation(db, principal, conversation_id)
        task = self._turn_tasks.get(conversation_id)
        if task and not task.done():
            task.cancel()
        async with self.writer_lock_for(conversation_id):
            async with SessionLocal() as db:
                conversation = await self.get_owned_conversation(db, principal, conversation_id)
                active = self._answer_streams.pop(conversation_id, None)
                if active and active[0].content:
                    stream, language = active
                    conversation.last_seq = max(conversation.last_seq, stream.last_seq)
                    await stream.verified(stream.content + cancelled_stream_notice(language))
                    conversation.last_seq = max(conversation.last_seq, stream.last_seq)
                    await self.append_audit(db, conversation, "answer.stream", {
                        "streamVersion": stream.protocol_version, "status": "cancelled",
                        "blockCount": stream.block_count, "blockReceipts": stream.block_receipts,
                    }, request_id=principal.request_id, runtime_id=conversation.runtime_id)
                    await self.append_event(db, conversation, "assistant.message", {
                        "content": stream.content, "streamVersion": stream.protocol_version,
                        "streamStatus": "cancelled", "requestId": principal.request_id,
                    })
                conversation.status = "READY"
                await self.append_event(db, conversation, "turn.cancelled", {"requestId": principal.request_id})
                await db.commit()

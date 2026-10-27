import json
import re
from dataclasses import dataclass
from typing import Any


def response_language_for(text: str) -> str:
    """Use Arabic only for primarily Arabic input; English is the fallback."""

    arabic_count = sum(
        1
        for char in text
        if "\u0600" <= char <= "\u06ff"
        or "\u0750" <= char <= "\u077f"
        or "\u08a0" <= char <= "\u08ff"
        or "\ufb50" <= char <= "\ufdff"
        or "\ufe70" <= char <= "\ufeff"
    )
    latin_count = sum(1 for char in text if ("A" <= char <= "Z") or ("a" <= char <= "z"))
    return "ar" if arabic_count > latin_count else "en"


@dataclass(frozen=True)
class SkillRoute:
    skill_id: str
    category: str
    tool_name: str | None = None
    mode: str = "answer"
    fields: tuple[str, ...] = ()
    choices: tuple[str, ...] = ()
    confirmation_required: bool = False
    routing_locked: bool = False


LEGACY_SKILL_ID_MIGRATIONS = {
    "payment_receipt": "payment_transaction_history",
    "application_payment": "application_payment_details",
    "fine_payment": "fine_payment_guidance",
}


def canonical_skill_id(skill_id: str) -> str:
    """Normalize retired built-in IDs in routing context and audit metadata."""

    return LEGACY_SKILL_ID_MIGRATIONS.get(skill_id, skill_id)


def _guidance(when: str, boundary: str, prerequisites: str, response: str, navigation: str = "") -> str:
    sections = [
        f"WHEN TO USE: {when}",
        f"DO NOT USE WHEN: {boundary}",
        f"PREREQUISITES: {prerequisites}",
        f"RESPONSE RULES: {response}",
    ]
    if navigation:
        sections.append(f"NAVIGATION: {navigation}")
    return "\n".join(sections)


CUSTOMER_FACING_KNOWLEDGE_EVIDENCE_POLICY = (
    "Use retrieved knowledge only as internal evidence. Do not display sources, references, citations, "
    "source/reference sections, retrieved-document links, or locators in the customer-facing answer."
)


CROSS_LANGUAGE_CONSISTENCY_RULES: tuple[str, ...] = (
    "First establish one language-neutral answer plan from the user's intent and the trusted evidence, then render that same plan in the required response language.",
    "Across languages, preserve the same substantive facts, eligibility conditions, timelines, caveats, uncertainty, refusal boundaries, alternatives, section order, list count, and level of detail.",
    "Use the same answer outline whenever those sections apply: outcome, verified information, information needed, ordered steps, limitations, and next action. Do not omit a confirmed prerequisite, preview, confirmation condition, or limitation in one language.",
    "Do not add a policy claim, procedural rule, fee, deadline, exception, or technical explanation in one language unless the same claim is supported by the evidence and included in the language-neutral plan.",
    "For safety refusals, keep the refusal scope and safe alternatives equivalent; do not invent extra internal control mechanics in one language.",
    "Use only the required response language for explanatory prose, headings, list labels, and portal-action labels. Do not append bilingual translations or source-language labels in parentheses. Preserve only immutable identifiers, URLs, and exact legal proper names when necessary.",
    "Translate the approved answer faithfully rather than summarizing it differently. Keep proper names and links consistent, and state unconfirmed details as unconfirmed in every language.",
    "Treat standalone numbers, ordinals, and labels in the user message as context only unless they are verified business identifiers or requested fields; never repeat a stray number or explain that it maps to no option.",
    "Complete every sentence and list before sending; never end with an unfinished fragment, placeholder, or dangling question.",
)


def _knowledge_guidance(when: str, boundary: str, prerequisites: str, response: str, navigation: str = "") -> str:
    return _guidance(
        when,
        boundary,
        prerequisites,
        f"{response} {CUSTOMER_FACING_KNOWLEDGE_EVIDENCE_POLICY}",
        navigation,
    )


SKILL_GUIDANCE: dict[str, str] = {
    "document_ocr": _guidance(
        "the user attaches a PDF/image or asks to extract fields from a document.",
        "the user asks whether a document has already been submitted, approved, or verified by UMC.",
        "an attachment and a trusted caller identity; OCR output must be treated as unverified evidence.",
        "state that extracted values require user confirmation, preserve identifiers as strings, and report uncertainty or unreadable fields.",
    ),
    "service_discovery": _knowledge_guidance(
        "the user wants to find or compare a media service for a stated activity.",
        "the user asks for a personal application status, issued document status, fee transaction, or legal ruling.",
        "the applicant type and media activity; use knowledge evidence to identify candidate services.",
        "separate candidate services from legal eligibility and explain what information is still missing.",
    ),
    "umc_application_detail": _guidance(
        "the user selects one of their My Requests records and asks for its detail, timeline, fee, service, or linked document.",
        "no application identifier or selectable My Requests record is available, or the question concerns an issued License/Permit list.",
        "a current UMC bearer token and a positive applicationId.",
        "return only fields from live responses, identify the application number and current status, and never infer missing milestones or approval decisions. This Skill is read-only: do not edit, cancel, duplicate, submit, or pay.",
    ),
    "umc_book_by_isbn": _guidance(
        "the user asks to look up a book by ISBN.",
        "the identifier is not an ISBN or the user asks for a general book recommendation.",
        "the ISBN as a string, including leading zeros and hyphens until normalized.",
        "show only the record returned by UMC and state when no matching record is found.",
    ),
    "umc_add_application": _guidance(
        "the user explicitly asks to create or update a controlled draft in the test environment.",
        "the user asks to formally submit an application, pay, or bypass confirmation.",
        "trusted UMC identity, complete draft parameters, and explicit confirmation immediately before the write.",
        "preview the payload first; allow only type=3/isTest=true new drafts or type=2 updates with applicationId; never claim formal submission.",
    ),
    "copyright_guidance": _knowledge_guidance(
        "the user asks about copyright, content permission, or media content compliance.",
        "the user asks for a personal license status, a binding legal opinion, or a guarantee that a use is cleared.",
        "current knowledge-base evidence and the specific content scenario.",
        "distinguish general guidance from legal advice, and identify when professional review is needed.",
    ),
    "admin_inspection": _guidance(
        "an authorized administrator asks for inspection summaries, risk indicators, or drill-downs.",
        "a customer asks about another account or the caller lacks administrator scope.",
        "trusted principal and verified admin_scope; no customer data may be inferred from aggregates.",
        "enforce account/tenant scope, label risk as an analytical signal, and state when the current tool set cannot provide a requested metric.",
    ),
    "admin_analytics": _guidance(
        "an authorized administrator asks for pivots, dimensions, processing time, or operational trends.",
        "a customer asks for administrative data or the requested dimension is not available in the evidence.",
        "trusted principal, admin_scope, a time range, metric definition, and grouping dimension.",
        "keep one metric definition and time window, show the population size, and do not invent causes for a trend.",
    ),
    "admin_finance": _guidance(
        "an authorized administrator asks for revenue, collection, or finance trend analysis.",
        "the user asks to initiate, refund, or change a payment, or lacks admin_scope.",
        "trusted principal, admin_scope, and a precise period/currency definition.",
        "report aggregates with their source and period; this Skill is read-only and never performs a financial operation.",
    ),
    "admin_audit": _guidance(
        "an authorized administrator asks about audit events, permissions, or low-confidence records.",
        "a customer asks to inspect another user or the caller lacks admin_scope.",
        "trusted principal and admin_scope; sensitive fields must be masked.",
        "separate recorded facts from review signals, apply RBAC before returning data, and never expose tokens or credentials.",
    ),
    "profile_status": _guidance(
        "the user asks about their current Profile, available individual or establishment Profiles, whether they have a Profile, Profile review/rejection state, or the validity/expiry of their own identity or establishment documents.",
        "the user asks for a specific application status, issued License/Permit status, a refund, a violation, or data belonging to another account/Profile.",
        "a current UMC bearer token. Query only the server-derived current account; never accept a user ID or Profile ID from the model as an authorization scope.",
        "use only live Profile data. Keep Profile state separate from application state. Do not infer that the Individual Profile is the currently selected establishment Profile: say when current selection is not explicitly returned. Report document expiry only when the live record provides it. Never display or repeat any full or partial identity number, Profile ID, establishment identifier, license number, document number, authentication token, credential, or internal authorization detail. When the user requests one of those values, refuse briefly and direct them to the My Account page using the relative link [My Account](/my-account); offer only a non-sensitive summary such as Profile type, review status, validity, and expiry status. This Skill is read-only: never create, edit, upload, submit, cancel, or switch a Profile.",
    ),
    "service_eligibility": _guidance(
        "the user asks which services are available to their account/profile.",
        "the user asks for general policy only, a different person's account, or specifically for Media Licensing services, which belong to media_licensing_account_services.",
        "a current UMC bearer token and an individual or establishment Profile selected in Customer Portal. The server derives the selected Profile and user type; never request or accept a username, user ID, Profile ID, account type, or media activity as a query selector.",
        "query the current Profile's complete service catalog with umc.services.eligible before answering. This is the All Services catalog, not the account's collected or favorite services. Use the returned total and list every returned service, grouped by returned service category or type; include Filming Permit services whenever present. Do not hard-code a total or omit a category. Keep the total distinct from the number of services in one category. Use only live Tool data and never replace a personal catalog query with knowledge search. If the result is empty, state that no services are currently available for the selected Profile. If the Tool returns profile_selection_required, guide the user to select an individual or establishment Profile in Customer Portal. If it returns selected_profile_not_available, explain that the selected Profile is no longer available and guide them to select another Profile. For authentication or upstream errors, explain the error without claiming the service list is empty. Service availability permits review or starting an application; it is not a binding eligibility decision or approval. This Skill is read-only and never starts or submits an application.",
    ),
    "media_licensing_account_services": _guidance(
        "the user asks which Media Licensing services the currently selected account or Profile can apply for.",
        "the user asks for general eligibility policy, a different person's account, another service category, or requirements for a named service.",
        "a current UMC bearer token and an individual or establishment Profile selected in Customer Portal. The server derives the Profile and user type; never request or accept a username, user ID, Profile ID, account type, or media activity as a query selector.",
        "always use the live account-scoped Tool result and never fall back to the knowledge base. If services are returned, list their names and only fields present in the result. If the result is empty, explicitly state that no Media Licensing services are available for the currently selected Profile. If the Tool returns profile_selection_required, tell the user to select an individual or establishment Profile in Customer Portal and retry. If it returns selected_profile_not_available, tell the user that the selected Profile is no longer available and to select another Profile. Never describe either Profile-selection error as temporary or recommend waiting and retrying without changing the Profile. Do not ask the user to repeat identity or Profile information and do not make a binding legal eligibility decision.",
    ),
    "application_payment": _guidance(
        "the user asks which My Requests applications are awaiting payment or asks to inspect the payment details for a selected application.",
        "the user asks to make, retry, or confirm a payment, or asks about an unrelated transaction or issued permit.",
        "trusted UMC identity; use the current application list and the selected application's read-only payment detail.",
        "first list only Pending Payment applications, then inspect payment detail only for an application selected from that list. Preserve that selected list item's Application No.; never label a numeric applicationId or serviceApplicationId from payment detail as an application number. Show application number, service, amount, currency, payment status, due date, and timeline only when returned by UMC. This Skill is read-only and never starts, retries, or confirms payment.",
    ),
    "application_status": _guidance(
        "the user asks for the status, progress, filters, counts, or history of their own My Requests applications.",
        "the user asks for an issued license/permit count or general application requirements.",
        "trusted UMC identity; query the current account's application list and use detail only for a selected application.",
        "include Application No., Service Name, Request Type, Profile Name, Submission Time, current status, and result scope when returned by UMC. A four-part UMC reference such as HC-02-2026-3194244 or MC-3-203-2852058 is an application number: always query the current account's live application list with that exact keyword and never use the knowledge base. If an exact match is returned, report its available status or stage; if no exact match is returned for the selected Profile, say so and guide the user to [My Requests](/my-requests). Keep Request Type separate from status and call an application a renewal only when Request Type is Renew. This Skill is read-only and never edits, cancels, duplicates, submits, or pays.",
    ),
    "license_permit_status": _guidance(
        "the user asks about their own issued License/Permit list, count, status, validity, expiry, number, or available portal actions, including a named document such as 'How about my Social Media Advertiser Permit?'.",
        "the user explicitly asks for renewal process/requirements, My Requests application status, pending payment, a new-license application, or administrative records.",
        "a current UMC bearer token and live License/Permit APIs; query the current account's issued-record list and never substitute application records.",
        "for a named permit, match it against the returned issued records and report its actual status, effective date, expiry date, number, and available actions. Do not say account access is unavailable when the live lookup succeeds. Do not use public verification or knowledge-base guidance as a substitute for the account result. This Skill is read-only and never renews, modifies, cancels, transfers, or submits.",
    ),
    "license_permit_modification_knowledge": _knowledge_guidance(
        "the user asks whether a current License/Permit can be modified, why its Modify action is unavailable, or asks the general modification scope, requirements, documents, fees, or process.",
        "the user asks to actually create, save, submit, cancel, transfer, or pay for a modification; asks for renewal, download, or a My Requests application status.",
        "for a named current document, the live issued-record list; for general process questions, current knowledge evidence.",
        "state the live availability of Modify separately from general guidance. Use only returned actions and evidence; do not invent supported changes or claim a modification was started. Direct the user to the selected record's Modify action when it is available.",
    ),
    "permit_download": _guidance(
        "the user asks to view or download a specific issued License/Permit document.",
        "the user has not selected a document, or asks to download an application draft or another user's document.",
        "the customer uses the authenticated NMA customer portal.",
        "explain that the customer must download the issued record from the Licenses & Permits page in the portal. The downloaded PDF is protected and requires an Access Code to open. The assistant must not download or open the file for the customer, retrieve, request, or reveal an Access Code, document URL, or download credential. Give concise portal guidance in the user's current language and direct the customer to official support if they cannot access the code.",
    ),
    "payment_receipt": _guidance(
        "the user asks to find or download a receipt for a completed payment.",
        "the transaction is pending/failed, or the user asks to make or reverse a payment.",
        "trusted UMC identity and a selected successful transaction number.",
        "show the transaction first, confirm the selected receipt, and do not fabricate a receipt URL or payment success.",
    ),
    "fine_appeal": _guidance(
        "the user asks to challenge a specific media violation.",
        "no violation is identified, or the user asks to pay the fine instead.",
        "the current account's pending violations; a violation number, appeal reason, and details are required only to prepare a later submission.",
        "first check whether a current violation can be appealed and state the returned eligibility and deadline. For a later submission, show a complete preview, keep the submitted reason/details verbatim where possible, obtain explicit confirmation, and never claim the appeal was filed without a successful write response.",
    ),
    "complaint_create": _guidance(
        "the user wants to prepare a complaint about a delayed application.",
        "the user asks to complain about a different topic or provides no application reference.",
        "application number, complaint details, and the available enquiry type/application options.",
        "create a preview only; obtain explicit confirmation before any future write integration and never claim a complaint was submitted.",
    ),
    "enquiry_followup": _guidance(
        "the user wants to follow up on an existing enquiry.",
        "no enquiry reference exists, or the user wants to create a new complaint.",
        "trusted UMC identity and the current account's enquiry list; an enquiry reference is needed only when more than one enquiry must be distinguished.",
        "first show the matching or most recent enquiry and its current state. Explain the available portal follow-up action without claiming that a follow-up was sent when only a read operation is available.",
    ),
    "enquiry_reopen": _guidance(
        "the user asks what can be done with a resolved enquiry.",
        "the user expects an unsupported one-click reopen operation.",
        "a resolved enquiry reference and the current account's enquiry record.",
        "first locate the enquiry by its reference, or list recent resolved enquiries when no reference is supplied. Explain the record's available action; never assume that a resolved enquiry is closed forever or promise reopening without an API result. If reopening is unavailable, offer the verified message or linked-enquiry alternative.",
    ),
    "technical_enquiry": _guidance(
        "the user reports a payment failure and wants a technical enquiry prepared.",
        "the user asks to retry or complete payment directly.",
        "the available enquiry types; transaction number, error message, and occurrence time are requested only when available.",
        "direct the customer to raise an Enquiry for a payment technical issue, preserving any error details and screenshots. Offer a concise technical-enquiry preview, but do not claim submission or payment resolution.",
    ),
    "license_application_knowledge": _knowledge_guidance(
        "the user asks how to apply for a named new media license or permit, including its requirements, documents, fees, or process.",
        "the user asks to create, save, submit, modify, pay for, or track an application; asks about an issued document or renewal; or only describes an activity without identifying a license/service.",
        "the named license/service and current knowledge evidence. For an explicit license or permit name, search knowledge before asking for applicant type.",
        "provide evidence-based requirements and process only. Keep service discovery for an unknown service/activity, and keep future application creation or submission in a separate write Skill. Do not invent a personalized approval outcome.",
    ),
    "service_eligibility_info": _knowledge_guidance(
        "the user asks for general information about who may use media services.",
        "the user asks for a personal eligibility determination or account/profile status.",
        "knowledge-base evidence and the applicant category/activity being discussed.",
        "label the answer as general guidance and direct personal checks to the profile/service eligibility flow.",
    ),
    "license_renewal": _knowledge_guidance(
        "the user asks which existing License/Permit needs attention or wants read-only renewal/expiry eligibility information.",
        "the user asks to apply for a new service or merely wants the current document count/status.",
        "the document type/number when account lookup is needed, current action-needed data, and the renewal knowledge rule.",
        "for a general expiring-soon question, retrieve the renewal rule before asking for a document number; for an account-specific action check, use current action-needed data. Distinguish renewal from extension/modification, use action validation only as a read-only eligibility check, and never initiate renewal or another write operation. Ask for confirmation only if a later download is requested.",
    ),
    "my_requests_pending_actions": _guidance(
        "the user asks what is pending, what needs attention, or which actions are outstanding in My Requests.",
        "the user asks about issued License/Permit expiry actions, administrative review data, or asks to perform an action.",
        "trusted UMC identity and the current account's pending-actions/application list.",
        "list only live pending items, identify the related application and status, and do not pay, edit, cancel, duplicate, submit, or otherwise mutate a request.",
    ),
    "service_fees": _knowledge_guidance(
        "the user asks for the fee or processing time of a specific media service.",
        "the service is not identified, or the user asks for a personal payment/receipt.",
        "service name or ID plus current knowledge evidence and effective-date context.",
        "disambiguate service IDs, show currency and effective date, and never combine fees from different services.",
    ),
    "latest_regulations": _knowledge_guidance(
        "the user asks for current media regulations, a Cabinet Resolution, or an exact quotation.",
        "the requested instrument/article cannot be identified, or the user asks for a personal legal clearance.",
        "knowledge evidence; exact quotation additionally requires the instrument number/title/date and article or clause.",
        "distinguish summary from quotation, and ask a clarification question when the source is ambiguous.",
    ),
    "fine_payment": _knowledge_guidance(
        "the user asks about an unpaid media fine and how payment works.",
        "the user asks to appeal, or no violation can be identified.",
        "current pending-violation data and the payment procedure from the knowledge base.",
        "show the violation and amount returned by UMC, require explicit confirmation before payment, and never claim payment completion without a transaction result.",
    ),
    "general_knowledge": _knowledge_guidance(
        "no business domain was confidently recalled and the user asks for general NMA or media-service information.",
        "the user asks for a personal application, license, payment, account, or other live record that needs a business Skill.",
        "knowledge-base evidence; ask a focused follow-up only when the question cannot be answered reliably from the available evidence.",
        "answer from retrieved evidence and clearly state when the knowledge base has no reliable answer. Do not invent personal account results or policy requirements.",
    ),
}


# Routing metadata is intentionally short and operational. The full Skill
# content remains the source of answer-time instructions; these fields are
# used only for deterministic candidate recall before LLM classification.
SKILL_ROUTING_METADATA: dict[str, dict[str, Any]] = {
    "document_ocr": {"domain": "documents", "aliases": ["ocr", "document", "image", "attachment", "扫描件", "图片"]},
    "service_discovery": {"domain": "services", "aliases": ["find service", "compare services", "which service", "服务", "service catalogue"]},
    "umc_application_detail": {"domain": "applications", "aliases": ["application detail", "application id", "申请详情", "applicationid"]},
    "umc_book_by_isbn": {"domain": "books", "aliases": ["isbn", "book lookup", "书号"]},
    "umc_add_application": {"domain": "applications", "aliases": ["new application", "create draft", "新建申请", "草稿"]},
    "copyright_guidance": {"domain": "knowledge_policy", "aliases": ["copyright", "content permission", "版权", "内容合规"]},
    "admin_inspection": {"domain": "admin", "aliases": ["inspection", "risk", "检查", "高风险"]},
    "admin_analytics": {"domain": "admin", "aliases": ["analytics", "pivot", "processing time", "分析", "趋势"]},
    "admin_finance": {"domain": "admin", "aliases": ["revenue", "collection", "finance", "收入", "财务"]},
    "admin_audit": {"domain": "admin", "aliases": ["audit", "permissions", "审计", "权限"]},
    "profile_status": {"domain": "profile", "aliases": ["my profile", "profile review", "profile status", "profile expiry", "资料审核", "档案", "身份"]},
    "service_eligibility": {"domain": "services", "aliases": ["my eligibility", "eligible for", "资格", "适用服务"]},
    "media_licensing_account_services": {"domain": "services", "aliases": ["media licensing services for this account", "media licensing services for my account", "media licensing services available to this profile", "which media licensing services can this account apply for"]},
    "application_payment": {"domain": "payments", "aliases": ["pending payment", "pay application", "待付款", "付款申请"]},
    "my_requests_pending_actions": {"domain": "applications", "aliases": ["my requests pending", "pending actions", "what needs attention", "待处理事项", "待办申请"]},
    "application_status": {"domain": "applications", "aliases": ["application status", "application progress", "申请状态", "申请进度"]},
    "license_permit_status": {"domain": "licenses_permits", "aliases": ["my license", "my permit", "named permit", "license status", "permit status", "license count", "advertiser permit", "许可证", "牌照", "许可"]},
    "license_permit_modification_knowledge": {"domain": "licenses_permits", "aliases": ["modify license", "modify permit", "change license details", "update media license", "add license activity", "修改许可证", "修改牌照"]},
    "permit_download": {"domain": "licenses_permits", "aliases": ["download license", "download permit", "license document", "下载许可证"]},
    "payment_receipt": {"domain": "payments", "aliases": ["receipt", "payment receipt", "收据", "付款凭证"]},
    "fine_appeal": {"domain": "fines", "aliases": ["appeal fine", "violation appeal", "申诉罚款", "违规申诉"]},
    "complaint_create": {"domain": "enquiries", "aliases": ["complaint", "投诉", "delayed application"]},
    "enquiry_followup": {"domain": "enquiries", "aliases": ["follow up enquiry", "enquiry follow-up", "跟进咨询"]},
    "enquiry_reopen": {"domain": "enquiries", "aliases": ["reopen enquiry", "resolved enquiry", "重新打开咨询"]},
    "technical_enquiry": {"domain": "enquiries", "aliases": ["technical enquiry", "payment failed", "技术咨询", "支付失败"]},
    "license_application_knowledge": {"domain": "license_application", "aliases": ["apply for license", "apply for permit", "license application", "license requirements", "permit requirements", "new license", "申请许可证", "办理牌照"]},
    "service_eligibility_info": {"domain": "knowledge_policy", "aliases": ["who is eligible", "eligibility information", "谁有资格", "资格说明"]},
    "license_renewal": {"domain": "licenses_permits", "aliases": ["renew license", "renewal", "extend permit", "续期", "延期"]},
    "service_fees": {"domain": "knowledge_policy", "aliases": ["service fee", "processing time", "费用", "办理时间"]},
    "latest_regulations": {"domain": "knowledge_policy", "aliases": ["regulation", "cabinet resolution", "media rules", "法规", "条例"]},
    "fine_payment": {"domain": "fines", "aliases": ["pay fine", "unpaid fine", "罚款缴纳", "未缴罚款"]},
}


DEFAULT_SKILL_DEFINITIONS: tuple[dict[str, Any], ...] = (
    {
        "skill_id": "document_ocr",
        "name": "Document OCR and field extraction",
        "allowed_tools": ["ocr.layout_parsing"],
        "dependencies": ["ocr_gateway", "trusted_principal"],
        "content": SKILL_GUIDANCE["document_ocr"],
    },
    {
        "skill_id": "service_discovery",
        "name": "Service discovery and comparison",
        "allowed_tools": ["knowledge.search"],
        "dependencies": ["knowledge_gateway"],
        "content": SKILL_GUIDANCE["service_discovery"],
    },
    {
        "skill_id": "umc_application_detail",
        "name": "UMC application detail lookup",
        "allowed_tools": ["umc.application_detail"],
        "dependencies": ["trusted_principal", "umc_customer_api"],
        "content": SKILL_GUIDANCE["umc_application_detail"],
    },
    {
        "skill_id": "umc_book_by_isbn",
        "name": "UMC book ISBN lookup",
        "allowed_tools": ["umc.book_by_isbn"],
        "dependencies": ["trusted_principal", "umc_customer_api"],
        "content": SKILL_GUIDANCE["umc_book_by_isbn"],
    },
    {
        "skill_id": "umc_add_application",
        "name": "UMC controlled draft application",
        "allowed_tools": ["umc.add_application"],
        "dependencies": ["trusted_principal", "umc_data_access", "explicit_confirmation"],
        "content": SKILL_GUIDANCE["umc_add_application"],
    },
    {
        "skill_id": "copyright_guidance",
        "name": "Copyright and content guidance",
        "allowed_tools": ["knowledge.search"],
        "dependencies": ["knowledge_gateway"],
        "content": SKILL_GUIDANCE["copyright_guidance"],
    },
    {
        "skill_id": "admin_inspection",
        "name": "Admin inspection drilldown",
        "allowed_tools": [],
        "dependencies": ["trusted_principal", "admin_scope"],
        "content": SKILL_GUIDANCE["admin_inspection"],
    },
    {
        "skill_id": "admin_analytics",
        "name": "Admin analytics and pivots",
        "allowed_tools": [],
        "dependencies": ["trusted_principal", "admin_scope"],
        "content": SKILL_GUIDANCE["admin_analytics"],
    },
    {
        "skill_id": "admin_finance",
        "name": "Admin finance trend",
        "allowed_tools": [],
        "dependencies": ["trusted_principal", "admin_scope"],
        "content": SKILL_GUIDANCE["admin_finance"],
    },
    {
        "skill_id": "admin_audit",
        "name": "Admin audit and permissions",
        "allowed_tools": [],
        "dependencies": ["trusted_principal", "admin_scope"],
        "content": SKILL_GUIDANCE["admin_audit"],
    },
    {
        "skill_id": "profile_status",
        "name": "My Profile status",
        "allowed_tools": ["umc.profile.summary"],
        "dependencies": ["trusted_principal", "umc_customer_api"],
        "workflow": {
            "defaultToolRequest": {"toolName": "umc.profile.summary", "arguments": {}},
        },
        "content": SKILL_GUIDANCE["profile_status"],
    },
    {
        "skill_id": "service_eligibility",
        "name": "Service eligibility",
        "allowed_tools": ["umc.services.eligible", "umc.service-categories"],
        "dependencies": ["trusted_principal", "umc_customer_api"],
        "workflow": {"defaultToolRequest": {"toolName": "umc.services.eligible", "arguments": {}}},
        "content": SKILL_GUIDANCE["service_eligibility"],
    },
    {
        "skill_id": "media_licensing_account_services",
        "name": "Media Licensing services for my Profile",
        "allowed_tools": ["umc.media-licensing.eligible-services"],
        "dependencies": ["trusted_principal", "umc_customer_api"],
        "workflow": {"defaultToolRequest": {"toolName": "umc.media-licensing.eligible-services", "arguments": {}}},
        "content": SKILL_GUIDANCE["media_licensing_account_services"],
    },
    {
        "skill_id": "application_payment",
        "name": "Pending payment application details",
        "allowed_tools": ["umc.applications", "umc.application_payment_detail"],
        "dependencies": ["trusted_principal", "umc_customer_api"],
        "workflow": {
            "routing": {
                "defaultIntentId": "list",
                "intents": [
                    {"id": "list", "description": "List the user's Pending Payment applications."},
                    {"id": "detail", "description": "Show payment detail for an application selected from the preceding Pending Payment list."},
                ],
                "filters": {
                    "record": {"type": "selection", "description": "A record from the latest Pending Payment application list, by ordinal or application number."},
                },
            },
            "requests": [
                {
                    "intentId": "list",
                    "toolName": "umc.applications",
                    "arguments": {"pageIndex": 1, "pageSize": 100, "applicationStatusId": "103", "sortBy": "createdOn", "sortDirection": 0},
                },
            ],
            "defaultToolRequest": {
                "toolName": "umc.applications",
                "arguments": {"pageIndex": 1, "pageSize": 100, "applicationStatusId": "103", "sortBy": "createdOn", "sortDirection": 0},
            },
            "selection": {
                "intentId": "detail",
                "filter": "record",
                "sourceTool": "umc.applications",
                "itemsPath": "data.applicationPage.items",
                "valueField": "id",
                "identifierFields": ["applicationNumber", "applicationNo"],
                "ordinalTerms": {
                    "1": ["first", "1st", "第一个", "第一笔", "الأول"],
                    "2": ["second", "2nd", "第二个", "第二笔", "الثاني"],
                },
                "toolRequest": {
                    "when": {"anyTerms": ["payment detail", "payment details", "付款详情", "支付详情", "تفاصيل الدفع"]},
                    "toolName": "umc.application_payment_detail",
                    "argumentName": "applicationId",
                    "argumentValueType": "integer",
                },
            },
        },
        "content": SKILL_GUIDANCE["application_payment"],
    },
    {
        "skill_id": "my_requests_pending_actions",
        "name": "My Requests pending actions",
        "allowed_tools": ["umc.pending-actions", "umc.applications"],
        "dependencies": ["trusted_principal", "umc_customer_api"],
        "workflow": {
            "routing": {
                "defaultIntentId": "list",
                "intents": [
                    {"id": "list", "description": "Show only the current My Requests actions that need attention."},
                ],
                "filters": {},
            },
            "requests": [{"intentId": "list", "toolName": "umc.pending-actions", "arguments": {}}],
            "defaultToolRequest": {"toolName": "umc.pending-actions", "arguments": {}},
        },
        "content": SKILL_GUIDANCE["my_requests_pending_actions"],
    },
    {
        "skill_id": "application_status",
        "name": "Application status",
        "allowed_tools": ["umc.applications", "umc.application_detail"],
        "dependencies": ["trusted_principal", "umc_customer_api"],
        "workflow": {
            "routing": {
                "defaultIntentId": "list",
                "intents": [
                    {"id": "list", "description": "Search or summarize the user's My Requests applications."},
                    {"id": "detail", "description": "Show a selected application from the preceding application list."},
                ],
                "filters": {
                    "keyword": {"type": "string", "description": "Application number or text the user explicitly supplied."},
                    "submissionDate": {"type": "date_range", "description": "Inclusive submission-date range with ISO start and end dates."},
                    "status": {
                        "type": "enum",
                        "description": "My Requests status filter.",
                        "options": [
                            {"id": "draft", "value": "101", "description": "Draft requests."},
                            {"id": "under_review", "value": "102", "description": "Requests under review."},
                            {"id": "pending_payment", "value": "103", "description": "Requests awaiting payment."},
                            {"id": "pending_modification", "value": "104", "description": "Requests awaiting modification."},
                            {"id": "completed", "value": "105", "description": "Completed requests."},
                            {"id": "rejected", "value": "106", "description": "Rejected requests."},
                            {"id": "cancelled", "value": "107", "description": "Cancelled requests."},
                            {"id": "pending_disposition", "value": "108", "description": "Requests pending disposition."},
                            {"id": "submitted", "value": "110", "description": "Submitted requests."},
                        ],
                    },
                    "record": {"type": "selection", "description": "A record from the latest application list, by ordinal or application identifier."},
                },
            },
            "textFilterBindings": [
                {
                    "filter": "keyword",
                    "pattern": r"\b[A-Z]{2,6}(?:-\d{1,8}){3,}\b",
                },
            ],
            "requests": [
                {
                    "intentId": "list",
                    "toolName": "umc.applications",
                    "arguments": {"pageIndex": 1, "pageSize": 100, "sortBy": "createdOn", "sortDirection": 0},
                    "bindings": [
                        {"filter": "keyword", "argument": "keyword"},
                        {"filter": "submissionDate.start", "argument": "startTime"},
                        {"filter": "submissionDate.end", "argument": "endTime"},
                        {"filter": "status", "argument": "applicationStatusId"},
                    ],
                },
            ],
            "defaultToolRequest": {
                "toolName": "umc.applications",
                "arguments": {"pageIndex": 1, "pageSize": 100, "sortBy": "createdOn", "sortDirection": 0},
                "bindings": [
                    {"filter": "keyword", "argument": "keyword"},
                    {"filter": "submissionDate.start", "argument": "startTime"},
                    {"filter": "submissionDate.end", "argument": "endTime"},
                    {"filter": "status", "argument": "applicationStatusId"},
                ],
            },
            "selection": {
                "intentId": "detail",
                "filter": "record",
                "sourceTool": "umc.applications",
                "itemsPath": "data.applicationPage.items",
                "valueField": "id",
                "identifierFields": ["applicationNumber", "applicationNo"],
                "toolRequest": {"toolName": "umc.application_detail", "argumentName": "applicationId", "argumentValueType": "integer"},
            },
        },
        "content": SKILL_GUIDANCE["application_status"],
    },
    {
        "skill_id": "license_permit_status",
        "name": "Issued license and permit status",
        "allowed_tools": ["umc.licenses.list", "umc.licenses.statistics", "umc.licenses.action_needed", "umc.licenses.detail"],
        "dependencies": ["trusted_principal", "umc_customer_api"],
        "workflow": {
            "routing": {
                "defaultIntentId": "list",
                "intents": [
                    {"id": "list", "description": "List or summarize issued licenses and permits."},
                    {"id": "named_permit", "description": "The user asks about a particular issued permit or license in their account, including 'How about my <permit name>?'."},
                    {"id": "expired", "description": "Records whose status is already expired."},
                    {"id": "expiring_soon", "description": "Records whose status is Expiring Soon, not already expired."},
                    {"id": "action_needed", "description": "Records returned by the portal's Action Needed feed."},
                    {"id": "detail", "description": "Show a selected record from the preceding license or permit list."},
                ],
                "filters": {
                    "record": {"type": "selection", "description": "A record from the latest license list, by ordinal or identifier."},
                },
            },
            "requests": [
                {
                    "intentId": "list",
                    "toolName": "umc.licenses.list",
                    "arguments": {"statuses": [], "documentTypes": [], "pageIndex": 1, "pageSize": 100, "sortDirection": 1},
                },
                {
                    "intentId": "named_permit",
                    "toolName": "umc.licenses.list",
                    "arguments": {"statuses": [], "documentTypes": [], "pageIndex": 1, "pageSize": 100, "sortDirection": 1},
                },
                {
                    "intentId": "expired",
                    "toolName": "umc.licenses.list",
                    "arguments": {
                        "statuses": ["EXPIRED"],
                        "documentTypes": [],
                        "pageIndex": 1,
                        "pageSize": 100,
                        "sortBy": "expireDate",
                        "sortDirection": 1,
                    },
                },
                {
                    "intentId": "expiring_soon",
                    "toolName": "umc.licenses.list",
                    "arguments": {"statuses": ["EXPIRE_SOON", "205"], "documentTypes": [], "pageIndex": 1, "pageSize": 100, "sortBy": "expireDate", "sortDirection": 1},
                },
                {"intentId": "action_needed", "toolName": "umc.licenses.action_needed", "arguments": {}},
            ],
            "defaultToolRequest": {
                "toolName": "umc.licenses.list",
                "arguments": {"statuses": [], "documentTypes": [], "pageIndex": 1, "pageSize": 100, "sortDirection": 1},
            },
            "selection": {
                "intentId": "detail",
                "filter": "record",
                "sourceTool": "umc.licenses.list",
                "itemsPath": "data.items",
                "valueField": "sourceLicenseId",
                "identifierFields": ["documentId", "licensePermitNo", "showLicenseNumber", "mediaLicenseNumber", "documentName"],
                "ordinalTerms": {
                    "1": ["first", "1st", "第一", "第一个", "الأول"],
                    "2": ["second", "2nd", "第二", "第二个", "الثاني"],
                },
                "toolRequest": {
                    "toolName": "umc.licenses.detail",
                    "argumentName": "id",
                    "argumentValueType": "string",
                },
            },
        },
        "content": SKILL_GUIDANCE["license_permit_status"],
    },
    {
        "skill_id": "license_permit_modification_knowledge",
        "name": "License and permit modification knowledge",
        "allowed_tools": ["knowledge.search", "umc.licenses.list"],
        "dependencies": ["knowledge_gateway", "trusted_principal", "umc_customer_api"],
        "workflow": {
            "routing": {
                "defaultIntentId": "general_guidance",
                "intents": [
                    {"id": "current_document", "description": "The user names or selects a current issued license or permit and asks whether Modify is available."},
                    {"id": "general_guidance", "description": "The user asks the modification scope, requirements, documents, fees, or process without selecting a current document."},
                ],
            },
            "requests": [
                {
                    "intentId": "current_document",
                    "toolName": "umc.licenses.list",
                    "arguments": {"statuses": [], "documentTypes": [], "pageIndex": 1, "pageSize": 100, "sortDirection": 1},
                },
            ],
            "toolRequestRules": [
                {
                    "when": {
                        "anyTerms": [
                            "my ", "this ", "my license", "my licence", "my permit", "this license", "this licence", "this permit",
                            "license number", "licence number", "permit number", "我的许可证", "我的牌照", "这个许可证", "这个许可",
                        ],
                    },
                    "toolName": "umc.licenses.list",
                    "arguments": {"statuses": [], "documentTypes": [], "pageIndex": 1, "pageSize": 100, "sortDirection": 1},
                },
            ],
            "defaultToolRequest": {"toolName": "knowledge.search", "arguments": {}},
        },
        "content": SKILL_GUIDANCE["license_permit_modification_knowledge"],
    },
    {
        "skill_id": "permit_download",
        "name": "Issued permit download",
        "allowed_tools": [],
        "dependencies": [],
        "content": SKILL_GUIDANCE["permit_download"],
    },
    {
        "skill_id": "payment_receipt",
        "name": "Payment receipt",
        "allowed_tools": ["umc.payments"],
        "dependencies": ["trusted_principal", "umc_customer_api"],
        "content": SKILL_GUIDANCE["payment_receipt"],
    },
    {
        "skill_id": "fine_appeal",
        "name": "Fine appeal",
        "allowed_tools": ["umc.pending-violations", "umc.appeal-reasons"],
        "dependencies": ["trusted_principal", "umc_customer_api"],
        "workflow": {"defaultToolRequest": {"toolName": "umc.pending-violations", "arguments": {}}},
        "content": SKILL_GUIDANCE["fine_appeal"],
    },
    {
        "skill_id": "complaint_create",
        "name": "Delayed application complaint",
        "allowed_tools": ["umc.enquiry-types", "umc.enquiry-applications"],
        "dependencies": ["trusted_principal", "umc_customer_api"],
        "content": SKILL_GUIDANCE["complaint_create"],
    },
    {
        "skill_id": "enquiry_followup",
        "name": "Enquiry follow-up",
        "allowed_tools": ["umc.enquiries"],
        "dependencies": ["trusted_principal", "umc_customer_api"],
        "workflow": {"defaultToolRequest": {"toolName": "umc.enquiries", "arguments": {}}},
        "content": SKILL_GUIDANCE["enquiry_followup"],
    },
    {
        "skill_id": "enquiry_reopen",
        "name": "Resolved enquiry follow-up",
        "allowed_tools": ["umc.enquiries"],
        "dependencies": ["trusted_principal", "umc_customer_api"],
        "workflow": {"defaultToolRequest": {"toolName": "umc.enquiries", "arguments": {}}},
        "content": SKILL_GUIDANCE["enquiry_reopen"],
    },
    {
        "skill_id": "technical_enquiry",
        "name": "Payment technical enquiry",
        "allowed_tools": ["umc.enquiry-types", "umc.payments"],
        "dependencies": ["trusted_principal", "umc_customer_api"],
        "workflow": {"defaultToolRequest": {"toolName": "umc.enquiry-types", "arguments": {}}},
        "content": SKILL_GUIDANCE["technical_enquiry"],
    },
    {
        "skill_id": "license_application_knowledge",
        "name": "Media license application knowledge",
        "allowed_tools": ["knowledge.search"],
        "dependencies": ["knowledge_gateway", "trusted_principal"],
        "content": SKILL_GUIDANCE["license_application_knowledge"],
    },
    {
        "skill_id": "service_eligibility_info",
        "name": "General media service eligibility knowledge",
        "allowed_tools": ["knowledge.search"],
        "dependencies": ["knowledge_gateway"],
        "content": SKILL_GUIDANCE["service_eligibility_info"],
    },
    {
        "skill_id": "license_renewal",
        "name": "License and permit action-needed information",
        "allowed_tools": ["knowledge.search", "umc.licenses.list", "umc.licenses.action_needed", "umc.licenses.action_validate", "umc.licenses.detail"],
        "dependencies": ["knowledge_gateway", "trusted_principal", "umc_customer_api"],
        "workflow": {"defaultToolRequest": {"toolName": "umc.licenses.action_needed", "arguments": {}}},
        "content": SKILL_GUIDANCE["license_renewal"],
    },
    {
        "skill_id": "service_fees",
        "name": "Media service fees",
        "allowed_tools": ["knowledge.search"],
        "dependencies": ["knowledge_gateway"],
        "content": SKILL_GUIDANCE["service_fees"],
    },
    {
        "skill_id": "latest_regulations",
        "name": "Latest media regulations and exact quotation",
        "allowed_tools": ["knowledge.search"],
        "dependencies": ["knowledge_gateway"],
        "content": SKILL_GUIDANCE["latest_regulations"],
    },
    {
        "skill_id": "fine_payment",
        "name": "Fine payment",
        "allowed_tools": ["umc.pending-violations", "knowledge.search"],
        "dependencies": ["trusted_principal", "knowledge_gateway", "umc_customer_api", "explicit_confirmation"],
        "content": SKILL_GUIDANCE["fine_payment"],
    },
    {
        "skill_id": "general_knowledge",
        "name": "General knowledge-base guidance",
        "allowed_tools": ["knowledge.search"],
        "dependencies": ["knowledge_gateway"],
        "domain": "general",
        "aliases": [],
        "positive_examples": [],
        "negative_examples": [],
        "content": SKILL_GUIDANCE["general_knowledge"],
    },
)

# Keep the definition shape backwards compatible while making routing metadata
# available to DB seeding, Redis catalog generation, and API clients.
for _definition in DEFAULT_SKILL_DEFINITIONS:
    _routing = SKILL_ROUTING_METADATA.get(_definition["skill_id"], {})
    _definition.update(_routing)
    _definition.setdefault("positive_examples", list(_routing.get("aliases", []))[:4])
    _definition.setdefault("negative_examples", [])
    _definition.setdefault("workflow", {})


ROUTING_RULES: dict[str, list[dict[str, Any]]] = {
    "document_ocr": [{"priority": 1000, "anyTerms": ["ocr", "optical character", "attached document", "attached Arabic trade license", "upload", "image", "screenshot", "IMG-", "识别材料", "扫描件", "图片", "截图", "上传", "رخصة التجارة العربية", "المرفقة", "صورة", "لقطة شاشة"], "route": {"category": "api_call", "mode": "collect", "fields": ["file", "file_type"]}}],
    "umc_book_by_isbn": [{"priority": 1000, "anyTerms": ["book by isbn", "isbn lookup", "isbn 查询", "isbn"], "route": {"category": "api_call", "fields": ["isbn"]}}],
    "umc_application_detail": [{"priority": 1000, "anyTerms": ["application detail", "applicationdetail", "application id", "applicationid", "申请详情", "申请 id"], "route": {"category": "api_call", "fields": ["applicationId"]}}],
    "umc_add_application": [{"priority": 1000, "anyTerms": ["add new application", "new draft application", "create a draft application", "新增申请", "新建草稿"], "route": {"category": "api_call", "mode": "collect", "fields": ["parameters"], "confirmationRequired": True}}],
    "application_payment": [
        {"priority": 990, "anyTerms": ["pending payment", "waiting for payment", "awaiting payment", "application payment", "application payments", "待付款", "支付多少钱", "付款详情", "الدفع المعلق", "كم أدفع", "تفاصيل الدفع"], "route": {"category": "data_query", "routingLocked": True}},
        {"priority": 980, "anyTerms": ["pay now", "how much to pay", "need to pay", "pay for", "payment details"], "anyTermGroups": [["application"], ["request"], ["申请"], ["请求"]], "route": {"category": "data_query", "routingLocked": True}},
    ],
    "payment_receipt": [
        {"priority": 970, "anyTerms": ["refund", "退款", "استرداد"], "noneTerms": ["refund in progress", "refund completed", "failed refund", "refund status", "退款中", "退款完成", "交易状态", "fine", "violation penalty", "unpaid fines", "罚款", "违规", "غرامة", "مخالفة"], "route": {"category": "portal_action", "mode": "portal_action", "routingLocked": True}},
        {"priority": 970, "anyTerms": ["download", "下载", "تنزيل", "تحميل", "export", "导出", "تصدير"], "anyTermGroups": [["receipt"], ["收据"], ["إيصال"], ["transaction"], ["交易"], ["record"], ["记录"], ["payment"], ["付款"], ["支付"]], "route": {"category": "portal_action", "mode": "portal_action", "routingLocked": True}},
        {"priority": 900, "anyTerms": ["payment", "transaction", "receipt", "付款", "支付", "交易", "收据", "إيصال", "إيصال الدفع"], "route": {"category": "data_query", "routingLocked": True}},
    ],
    "technical_enquiry": [{"priority": 995, "anyTerms": ["technical enquiry", "technical inquiry", "payment technical issue", "cannot complete payment", "technical support", "技术咨询", "无法完成支付", "استفسار تقني", "تعذر الدفع", "لا أستطيع إتمام الدفع"], "route": {"category": "api_call", "routingLocked": True}}],
    "fine_appeal": [{"priority": 965, "anyTerms": ["appeal", "challenge", "申诉", "申诉期限", "استئناف", "طعن"], "anyTermGroups": [["fine"], ["violation"], ["violation penalty"], ["fine notification"], ["罚款"], ["违规"], ["违章"], ["غرامة"], ["مخالفة"]], "route": {"category": "data_query", "fields": ["violation_number"], "routingLocked": True}}],
    "fine_payment": [{"priority": 960, "anyTerms": ["fine", "violation penalty", "unpaid fines", "罚款", "违规", "غرامة", "مخالفة"], "route": {"category": "data_query", "routingLocked": True}}],
    "latest_regulations": [
        {"priority": 880, "anyTerms": ["quote", "exactly", "逐字", "原文", "اقتبس", "حرفيًا", "النص الأصلي"], "route": {"category": "knowledge", "toolName": "knowledge.search", "mode": "exact_quote", "fields": ["regulation_or_resolution_reference", "article_number"], "choices": ["Latest updates", "Summary", "Exact quotation"]}},
        {"priority": 870, "anyTerms": ["regulation", "media rules", "media regulations", "cabinet resolution", "法规", "条例", "لائحة", "لوائح", "تشريعات", "قرار مجلس الوزراء", "content standards", "advertising on social media", "media content", "child-safety", "child safety", "children", "advertising rules", "policy comparison", "regulation version", "معايير المحتوى", "سلامة الأطفال"], "route": {"category": "knowledge", "toolName": "knowledge.search", "mode": "summary", "fields": ["regulation_topic", "regulation_or_resolution_reference", "article_number"], "choices": ["Latest updates", "Summary", "Exact quotation"]}},
    ],
    "my_requests_pending_actions": [{"priority": 850, "anyTerms": ["pending actions", "what needs attention", "outstanding actions", "my requests pending", "my pending actions", "待处理事项", "待办申请", "我的待办", "我的待处理", "需要处理"], "noneTerms": ["license", "licence", "permit", "许可证", "牌照", "许可", "رخصة", "تصريح"], "route": {"category": "data_query"}}],
    "license_renewal": [
        {"priority": 850, "anyTerms": ["expire", "expiring", "expiry", "到期", "ستنتهي", "منتهية"], "anyTermGroups": [["license"], ["licence"], ["permit"], ["许可证"], ["牌照"], ["许可"], ["رخصة"], ["تصريح"]], "noneTerms": ["which licenses", "which licences", "which permits", "how many", "number of licenses", "number of licences", "哪些许可证", "多少许可证"], "route": {"category": "knowledge", "toolName": "knowledge.search", "mode": "summary", "fields": ["license_or_permit_type"], "routingLocked": True}},
        {"priority": 860, "anyTerms": ["action needed", "actions needed", "needs renewal", "renewal due", "需要续期", "待处理"], "anyTermGroups": [["license"], ["licence"], ["permit"], ["许可证"], ["牌照"], ["许可"], ["رخصة"], ["تصريح"]], "route": {"category": "data_query", "fields": ["license_or_permit_type", "license_number"]}},
        {"priority": 855, "anyTerms": ["renew", "renewal", "extend an existing permit", "续期", "延期", "تجديد", "تمديد"], "route": {"category": "knowledge", "toolName": "knowledge.search", "fields": ["license_or_permit_type", "license_number"], "choices": ["Renew licence", "Extend permit"]}},
    ],
    "license_permit_modification_knowledge": [{"priority": 850, "anyTerms": ["modify", "modification", "change license", "change permit", "update license", "update permit", "修改", "变更"], "anyTermGroups": [["license"], ["licence"], ["permit"], ["许可证"], ["牌照"], ["许可"], ["رخصة"], ["تصريح"]], "route": {"category": "api_call", "fields": ["license_or_permit_type"]}}],
    "permit_download": [{"priority": 835, "anyTerms": ["download", "issued permit", "下载", "许可证", "تنزيل", "تحميل", "تصريح صادر"], "anyTermGroups": [["permit"], ["license"], ["许可"], ["تصريح"], ["رخصة"]], "route": {"category": "data_query"}}],
    "license_permit_status": [
        {"priority": 845, "anyTerms": ["my", "我的", "رخصتي", "تصريحي"], "anyTermGroups": [["license"], ["licence"], ["permit"], ["许可证"], ["牌照"], ["许可"], ["رخصة"], ["تصريح"]], "noneTerms": ["application", "申请", "طلب", "申请状态", "حالة الطلب", "download", "下载", "تنزيل", "تحميل"], "route": {"category": "data_query", "fields": ["license_or_permit_type"]}},
        {"priority": 830, "anyTerms": ["expire", "expiring", "expiry", "到期", "ستنتهي", "منتهية"], "anyTermGroups": [["license"], ["licence"], ["permit"], ["许可证"], ["牌照"], ["许可"], ["رخصة"], ["تصريح"]], "noneTerms": ["application", "申请", "طلب", "申请状态", "حالة الطلب"], "route": {"category": "data_query", "fields": ["license_or_permit_type"]}},
        {"priority": 830, "anyTerms": ["how many license", "how many licence", "number of licenses", "number of licences", "license status", "licence status", "permit status", "license expiry", "licence expiry", "expiring licenses", "expiring licences", "expiring permits", "my license", "my permit", "my licence", "my licenses", "my permits", "我的许可证", "我的牌照", "我的许可", "许可证数量", "牌照数量", "许可证状态", "牌照状态", "حالة رخصتي", "حالة تصريحي", "رخصتي", "تصريحي", "عدد الرخص", "عدد التصاريح"], "noneTerms": ["application", "申请", "طلب", "申请状态", "حالة الطلب"], "route": {"category": "data_query", "fields": ["license_or_permit_type"]}},
    ],
    "admin_inspection": [{"priority": 820, "anyTerms": ["inspection summary", "high risk", "inspection", "检查摘要", "高风险"], "route": {"category": "data_query"}}],
    "admin_analytics": [{"priority": 820, "anyTerms": ["dimension pivot", "process time", "按省", "按酋长国", "اتجاه وقت معالجة", "قسّمه حسب الإمارة"], "route": {"category": "data_query"}}],
    "admin_finance": [{"priority": 820, "anyTerms": ["revenue", "fine collection", "last 7 days", "finance trend", "收入", "罚款回收", "الإيرادات", "تحصيل الغرامات"], "route": {"category": "data_query"}}],
    "admin_audit": [{"priority": 820, "anyTerms": ["audit", "low-confidence", "full user details", "permissions", "审计", "低置信度", "用户详情"], "route": {"category": "data_query"}}],
    "application_status": [
        {"priority": 940, "patterns": [r"\b[A-Z]{2,6}(?:-\d{1,8}){3,}\b"], "route": {"category": "data_query", "routingLocked": True}},
        {"priority": 950, "patterns": ["application_number"], "route": {"category": "data_query", "routingLocked": True}},
        {"priority": 810, "anyTerms": ["latest status", "application status", "application history", "application histories", "status of my application", "what's the status of my application", "open applications", "summarize my open", "my requests", "我的申请", "我的请求", "申请状态", "申请进度", "申请历史", "حالة الطلب", "حالة طلبي", "آخر حالة"], "route": {"category": "data_query"}},
    ],
    "license_application_knowledge": [{"priority": 800, "anyTerms": ["how to apply", "how do i apply", "apply for", "application requirements", "license requirements", "permit requirements", "photography", "filming", "filming permit", "photocopying equipment", "advertising license", "new permit", "license application", "media license", "newspaper", "publication", "broadcasting", "radio", "television", "申请许可", "申请许可证", "办理牌照", "摄影", "طلب تصريح", "متطلبات الترخيص", "رخصة إعلامية", "تصريح", "ترخيص", "صحيفة", "منشور", "بث"], "route": {"category": "knowledge", "toolName": "knowledge.search", "fields": ["permit_or_service_type", "account_type"], "choices": ["Individual", "Commercial", "Government"]}}],
    "service_discovery": [{"priority": 790, "anyTerms": ["which service should", "very specific media activity", "not listed", "media service comparison", "difference between a photography permit and an advertiser permit", "apply for a media service", "advertiser permit", "paid product reviews", "social media", "right service", "service for my business", "服务对比", "未列出", "选择哪项服务", "找不到服务", "الخدمة المناسبة", "نشاطي التجاري"], "route": {"category": "knowledge", "toolName": "knowledge.search", "fields": ["account_type", "media_activity"]}}],
    "copyright_guidance": [{"priority": 875, "anyTerms": ["版权", "copyright", "photograph from the internet", "commercial campaign", "حقوق الطبع"], "route": {"category": "knowledge", "toolName": "knowledge.search", "mode": "summary", "fields": ["regulation_topic"]}}],
    "service_fees": [{"priority": 780, "anyTerms": ["cost", "processing time", "how much does", "service fees", "fees", "fee", "费用", "الرسوم"], "route": {"category": "knowledge", "toolName": "knowledge.search", "fields": ["service_name"]}}],
    "complaint_create": [{"priority": 770, "anyTerms": ["complaint", "投诉", "شكوى"], "route": {"category": "api_call", "mode": "collect", "fields": ["application_number", "complaint_details"], "confirmationRequired": True}}],
    "enquiry_followup": [{"priority": 770, "anyTerms": ["follow up", "enquiry", "咨询", "متابعة", "استفسار"], "anyTermGroups": [["earlier"], ["submitted"], ["跟进"], ["سابق"], ["مقدم"]], "route": {"category": "data_query", "fields": ["enquiry_reference"], "routingLocked": True}}],
    "enquiry_reopen": [{"priority": 770, "anyTerms": ["reopen", "resolved enquiry", "resolved inquiry", "重新打开", "重新开启", "إعادة فتح", "استفسار مغلق"], "route": {"category": "data_query", "fields": ["enquiry_reference"], "routingLocked": True}}],
    "profile_status": [{"priority": 880, "anyTerms": ["my profile", "profile status", "profile review", "profile is under review", "profile expired", "profile expiry", "my identity", "my establishment", "do i have a profile", "个人身份", "企业身份", "身份是否过期", "profile 审核", "资料审核", "档案状态", "الملف قيد المراجعة", "ملفي قيد المراجعة", "مراجعة الملف"], "route": {"category": "data_query", "routingLocked": True}}],
    "service_eligibility_info": [{"priority": 750, "anyTerms": ["who is eligible", "eligible for media services", "media service eligibility", "مؤهل", "الأهلية", "الخدمات الإعلامية"], "route": {"category": "knowledge", "toolName": "knowledge.search", "fields": ["account_type", "media_activity"], "choices": ["Individual", "Commercial", "Government"]}}],
    "service_eligibility": [
        {
            "priority": 1255,
            "allTerms": ["services"],
            "anyTermGroups": [["eligible", "apply"], ["can", "apply"], ["available"]],
            "patterns": [
                r"\b(?:my\s*(?:current\s+)?(?:profile|file|account)|(?:this|current)\s+(?:profile|file|account)|am\s+i|can\s+i|available\s+to\s+me)\b",
                r"^(?!.*\bmedia[\s-]+licen[sc](?:ing|es?)\b)",
            ],
            "route": {"category": "data_query", "toolName": "umc.services.eligible", "mode": "answer", "routingLocked": True},
        },
        {"priority": 825, "allTerms": ["service", "eligible", "apply"], "noneTerms": ["media licensing", "media-licensing", "media license", "media licence"], "route": {"category": "data_query", "routingLocked": True}},
        {"priority": 740, "anyTerms": ["eligible", "eligibility", "资格", "مؤهل", "الأهلية"], "noneTerms": ["media licensing", "media-licensing", "media license", "media licence"], "route": {"category": "data_query", "fields": ["profile_id", "account_type", "media_activity"], "choices": ["Individual", "Commercial", "Government"]}},
    ],
    "media_licensing_account_services": [
        {
            "id": "current-profile-media-licensing-catalogue-v1",
            "priority": 1260,
            "anyTermGroups": [["can", "apply"], ["eligible", "apply"], ["available"], ["show"], ["list"]],
            "patterns": [
                r"\bmedia[\s-]+licensing\s+services\b",
                r"\b(?:my\s*(?:(?:current|selected)\s+)?(?:profile|file|account)|(?:this|current|selected)\s+(?:profile|file|account)|can\s+i|am\s+i|available\s+to\s+me)\b",
            ],
            "noneTerms": [
                "general policy", "eligibility policy", "eligibility criteria", "requirements", "conditions",
                "documents", "processing time", "fees", "who can", "who is eligible", "how to apply", "how do i apply",
                "another account", "another profile", "another user", "different account", "different profile",
                "someone else", "other user", "someone's account", "someone's profile",
            ],
            "route": {"category": "data_query", "toolName": "umc.media-licensing.eligible-services", "mode": "answer", "routingLocked": True},
        },
        {"priority": 1260, "anyTerms": ["which media licensing services can this account apply for", "which media licensing services can my account apply for", "what media licensing services can this account apply for", "what media licensing services can my account apply for", "media licensing services available to this account", "media licensing services available to my account", "media licensing services available to this profile", "media licensing services available to my profile"], "route": {"category": "data_query", "routingLocked": True}},
    ],
}


for _definition in DEFAULT_SKILL_DEFINITIONS:
    rules = ROUTING_RULES.get(_definition["skill_id"])
    if rules:
        _definition["workflow"] = {**_definition["workflow"], "deterministicRouting": rules}


def _rule_matches(text: str, rule: dict[str, Any]) -> bool:
    lowered = text.lower()
    contains = lambda term: str(term).lower() in lowered
    any_terms = list(rule.get("anyTerms") or [])
    all_terms = list(rule.get("allTerms") or [])
    any_groups = list(rule.get("anyTermGroups") or [])
    if any_terms and not any(contains(term) for term in any_terms):
        return False
    if all_terms and not all(contains(term) for term in all_terms):
        return False
    if any_groups and not any(all(contains(term) for term in group) for group in any_groups if isinstance(group, list)):
        return False
    if any(contains(term) for term in list(rule.get("noneTerms") or [])):
        return False
    for pattern in list(rule.get("patterns") or []):
        if pattern == "application_number":
            if not re.search(r"\bML-\d+(?:-[A-Za-z0-9]+)*\b", text, re.IGNORECASE):
                return False
        elif not re.search(str(pattern), text, re.IGNORECASE):
            return False
    return bool(any_terms or all_terms or any_groups or rule.get("patterns"))


def resolve_configured_skill(
    text: str,
    definitions: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    canonicalize: bool = True,
) -> SkillRoute | None:
    """Evaluate persisted per-Skill routing rules without business-specific code."""

    candidates: list[tuple[int, int, str, dict[str, Any], dict[str, Any]]] = []
    for definition_index, definition in enumerate(definitions):
        workflow = dict(definition.get("workflow") or {})
        rules = workflow.get("deterministicRouting") or definition.get("deterministicRouting") or []
        skill_id = str(definition.get("skill_id") or definition.get("skillId") or "").strip()
        for rule_index, rule in enumerate(rules):
            if isinstance(rule, dict) and skill_id and _rule_matches(text, rule):
                candidates.append((int(rule.get("priority", 0)), -definition_index, skill_id, rule, dict(rule.get("route") or {})))
    if not candidates:
        return None
    _, _, skill_id, _, route = max(candidates, key=lambda candidate: (candidate[0], candidate[1]))
    return SkillRoute(
        canonical_skill_id(skill_id) if canonicalize else skill_id,
        str(route.get("category") or "data_query"),
        str(route["toolName"]) if route.get("toolName") else None,
        str(route.get("mode") or "answer"),
        tuple(str(value) for value in route.get("fields", []) or []),
        tuple(str(value) for value in route.get("choices", []) or []),
        bool(route.get("confirmationRequired", False)),
        bool(route.get("routingLocked", False)),
    )


def merged_skill_workflow(skill_id: str, published_workflow: dict[str, Any] | None) -> dict[str, Any]:
    """Fill missing legacy workflow sections from the canonical built-in Skill.

    Published values always win. This only preserves new workflow sections for
    records created before a Skill was renamed or its workflow was expanded.
    """

    def merge_defaults(default: Any, published: Any) -> Any:
        if not isinstance(default, dict) or not isinstance(published, dict):
            return published
        return {
            key: merge_defaults(default[key], value) if key in default else value
            for key, value in published.items()
        } | {
            key: value for key, value in default.items() if key not in published
        }

    canonical_id = canonical_skill_id(skill_id)
    default = next(
        (
            definition
            for definition in DEFAULT_SKILL_DEFINITIONS
            if canonical_skill_id(str(definition.get("skill_id") or "")) == canonical_id
        ),
        None,
    )
    return merge_defaults(dict((default or {}).get("workflow") or {}), dict(published_workflow or {}))


def resolve_skill(text: str) -> SkillRoute:
    """Resolve default Skill configuration; published Skills can override it at runtime."""

    return resolve_configured_skill(text, DEFAULT_SKILL_DEFINITIONS) or SkillRoute("general", "general")


def build_knowledge_query(route: SkillRoute, original_text: str) -> str:
    """Add domain anchors before retrieval so generic phrases do not hit unrelated corpora."""
    prefixes = {
        "license_application_knowledge": "UMC UAE Media Council media license permit application requirements process",
        "service_eligibility_info": "UMC UAE Media Council media services eligibility applicant types",
        "license_permit_status": "UMC UAE Media Council issued licenses permits current status expiry effective date",
        "license_permit_modification_knowledge": "UMC UAE Media Council media license permit modification requirements documents fees process",
        "license_renewal": "UMC UAE media license permit renewal extension validity fees process",
        "service_fees": "UMC UAE Media Council media service fees service ID",
        "latest_regulations": "UAE media regulation Federal Decree-Law 55 of 2023 Cabinet Decision 68 of 2024 article",
        "fine_payment_guidance": "UMC media violation fine payment process",
        "copyright_guidance": "UMC UAE media content copyright permission commercial campaign guidance",
        "service_discovery": "UMC UAE Media Council media services service catalogue applicant activity",
    }
    prefix = prefixes.get(route.skill_id)
    return f"{prefix} {original_text}" if prefix else original_text


def exact_quote_source_sufficient(text: str) -> bool:
    """An article number alone is not a source; require a named/numbered instrument."""
    lowered = text.lower()
    if not _has(lowered, "quote", "exactly", "逐字", "原文"):
        return True
    return bool(
        __import__("re").search(
            r"(?:federal|cabinet|decree|law|resolution|regulation|联邦|法令|决议|条例|قرار مجلس الوزراء).{0,40}(?:no\.?|number|رقم|第|\d{2,4})|(?:no\.?|number|رقم|第)\s*\d",
            lowered,
        )
    )


def build_flow_prompt(route: SkillRoute) -> dict[str, Any]:
    prompt = {
        "profile_status": "Query the current account's live Profile summary. Report only returned Individual and establishment Profile records, their available status/review fields, and document expiry fields. Do not ask the customer to provide a user or Profile ID and do not change their selected Profile.",
        "service_eligibility": "Query the complete live service catalog for the Profile selected in the current UMC session. Report the returned total and every available service, including all returned categories. Never ask for identity selectors and never treat a failed lookup as an empty catalog.",
        "application_payment": "Select the Pending Payment application to inspect its read-only payment details; this Skill never starts or confirms payment.",
        "my_requests_pending_actions": "Query the current account's My Requests pending actions and identify the related application; no mutation is allowed.",
        "application_status": "Provide the application number. If it is unavailable, query the latest applications and state the result scope.",
        "license_permit_status": "Query the current user's issued licenses and permits. Report separate License and Permit counts, statuses, effective dates, expiry dates, and available actions; do not mix in application records.",
        "license_permit_modification_knowledge": "For a named current document, query issued licenses and permits and report whether Modify is available without starting it. For general modification questions, provide knowledge-based requirements and process only.",
        "permit_download": "Do not download or open the document. Explain that the customer must use the Licenses & Permits portal page; its downloaded PDF requires an Access Code to open, which the assistant cannot retrieve, request, or reveal. Respond in the user's current language.",
        "payment_receipt": "Confirm whether to download the latest successful transaction receipt, or provide a transaction number.",
        "fine_appeal": "Provide the media violation and appeal reason; show a preview and obtain explicit confirmation before submission.",
        "fine_payment": "Provide the media violation to pay; show the amount and payment information and obtain explicit confirmation.",
        "complaint_create": "Provide the delayed application number and complaint details; show a preview before submission.",
        "enquiry_followup": "Provide the enquiry reference to follow up; do not claim completion when no record is available.",
        "enquiry_reopen": "Provide the resolved enquiry reference; do not promise that reopening is supported.",
        "technical_enquiry": "Provide the failed transaction, error message, and occurrence time to prepare a technical enquiry preview.",
        "service_discovery": "Describe the account type and media activity so I can identify candidate UMC media services.",
        "license_application_knowledge": "Provide the named licence or permit. If only the activity is known, use service discovery first.",
        "license_renewal": "Provide the licence or permit type to renew or extend; account lookup also requires the licence number.",
        "service_fees": "Provide the specific media service or service ID whose fees you need.",
        "latest_regulations": "Provide the subject or the regulation/Cabinet Resolution number, title, and date; verbatim quotation also requires a specific article or clause.",
        "umc_application_detail": "Provide applicationId; the lookup only returns data authorized by the current UMC Token.",
        "umc_book_by_isbn": "Provide the ISBN as a string; only display the book record returned by UMC.",
        "umc_add_application": "Confirm the draft parameters first. Only type=3 with isTest=true is allowed for controlled testing; formal submission is blocked.",
    }.get(route.skill_id, "Ask for the information required to continue.")
    return {
        "required": True,
        "prompt": prompt,
        "fields": list(route.fields),
        "choices": list(route.choices),
        "confirmationRequired": route.confirmation_required,
    }


def build_system_prompt(
    route: SkillRoute,
    *,
    evidence_available: bool,
    response_language: str = "en",
    operator_prompt: str = "",
    skill_content: str = "",
    profile_context: object | None = None,
    available_tools: list[dict[str, Any]] | None = None,
) -> str:
    guardrails = [
        "Use only trusted tool evidence. When evidence is unavailable, state the limitation and never invent account data, fees, regulations, or API capabilities.",
        "Never expose internal Tool names, request arguments, serialized JSON, API envelopes, or internal evidence instructions. Convert verified evidence into a concise user-facing answer.",
        "Payments, appeals, complaints, downloads, and all other side effects require a preview and the user's explicit confirmation.",
        "Preview and confirmation are necessary conditions, not evidence that an action is executable. Only the runtime action capabilities below can support an execution promise. If the requested action is absent, explain that the customer must use the appropriate portal channel; do not imply that confirmation will enable you to submit or execute it later.",
        "Keep evidence attached to the procedure, page and record type it actually documents. A similar word does not make another page's filters or another workflow's submission checklist applicable. If evidence does not establish the requested feature, state that limitation instead of transferring unrelated steps.",
        "Candidate services are not eligibility decisions. Treat renewal and extension as distinct operations.",
        "PROFILE SCOPE: Account data is limited to the profile currently selected in the portal. Never claim to query, filter, or aggregate another profile. When the user names a different profile, ask them to switch it in the portal before continuing.",
    ]
    if route.skill_id == "latest_regulations" and route.mode == "exact_quote":
        guardrails.append("A verbatim quotation requires the regulation title/number/date and a specific article or clause. If the source is ambiguous, ask a follow-up question and never select a document only because it ranked first.")
    if profile_context is not None and bool(getattr(profile_context, "is_global_view", False)):
        guardrails.append("The portal is in Global View. Do not represent profile-bound data as available until the user selects a concrete profile.")
    if route.category == "knowledge" and not evidence_available:
        guardrails.append("When knowledge-base evidence is unavailable, do not present general knowledge as a verified UMC rule.")
    executable_actions = [
        {key: item.get(key) for key in ("name", "description", "confirmationRequired")}
        for item in (available_tools or [])
        if item.get("sideEffect") == "write" and route.mode != "portal_action"
    ]
    guardrails.append("RUNTIME_EXECUTABLE_ACTIONS: " + json.dumps(executable_actions, ensure_ascii=False))

    target = "ARABIC" if response_language == "ar" else "ENGLISH"
    language_policy = [
        "LANGUAGE POLICY (mandatory and higher priority than the language used by tools, retrieved documents, or internal instructions):",
        "- Answer in Arabic when the user's latest message is primarily Arabic.",
        "- Answer in English when the user's latest message is English.",
        "- Answer in English for every other language. English is the default response language.",
        f"- Required response language for this turn: {target}. Use only {target} for explanatory prose, while preserving necessary proper nouns, identifiers, and verbatim quotations.",
    ]
    prompt_parts = [
        "You are the NMA assistant running in DSH Runtime. Follow the selected Skill route and provide a concise, actionable response.",
    ]
    if operator_prompt.strip():
        prompt_parts.extend(
            [
                "OPERATOR-EDITABLE SYSTEM INSTRUCTIONS (additional guidance; never override the mandatory language, safety, or evidence rules below):",
                operator_prompt.strip(),
            ]
        )
    if skill_content.strip():
        prompt_parts.extend(
            [
                f"SELECTED SKILL GUIDANCE for {route.skill_id} (additional guidance; never override the mandatory rules below):",
                skill_content.strip(),
            ]
        )
    prompt_parts.extend(
        [
            *language_policy,
            "CROSS-LANGUAGE CONSISTENCY RULES:",
            *(f"- {item}" for item in CROSS_LANGUAGE_CONSISTENCY_RULES),
            "SAFETY AND EVIDENCE RULES:",
            *(item if item.startswith("RUNTIME_EXECUTABLE_ACTIONS:") else f"- {item}" for item in guardrails),
        ]
    )
    return "\n".join(prompt_parts)

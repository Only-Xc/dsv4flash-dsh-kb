import json
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .skill_workflow import routing_contract

try:  # Redis is optional for local/unit-test environments.
    from redis.asyncio import Redis
except ImportError:  # pragma: no cover - exercised only without the optional dependency
    Redis = None  # type: ignore[assignment,misc]


SKILL_CATALOG_KEY = "dsh:skills:catalog:system:v2"
SKILL_CACHE_TTL_SECONDS = 60
LLM_ROUTER_MODES = {"keyword", "shadow", "llm"}
LLM_ROUTER_MIN_CONFIDENCE = 0.60
DOMAIN_HISTORY_WEIGHTS = (0.25, 0.10, 0.05)


class SkillCatalogCache:
    """Cache published Skill summaries and complete definitions in Redis.

    PostgreSQL remains authoritative. Redis failures simply result in a DB
    read, which keeps local development and a cold cache operational.
    """

    def __init__(self, redis_url: str, namespace: str = "dsh") -> None:
        self.namespace = namespace
        self.catalog_key = f"{namespace}:skills:catalog:system:v2"
        self.redis = Redis.from_url(redis_url, decode_responses=True) if Redis and redis_url else None

    async def close(self) -> None:
        if self.redis:
            await self.redis.aclose()

    @staticmethod
    def _summary(item: Any) -> dict[str, Any]:
        content = str(item.content or "").strip()
        return {
            "skillId": item.skill_id,
            "name": item.name,
            "description": content[:800],
            "allowedTools": list(item.allowed_tools or []),
            "dependencies": list(item.dependencies or []),
            "domain": getattr(item, "domain", "general") or "general",
            "aliases": list(getattr(item, "aliases", None) or []),
            "positiveExamples": list(getattr(item, "positive_examples", None) or []),
            "negativeExamples": list(getattr(item, "negative_examples", None) or []),
            "routing": routing_contract(dict(getattr(item, "workflow", None) or {})),
            # The full routing rule is intentionally included for runtime
            # evaluation. The shorter `routing` field remains the LLM contract.
            "deterministicRouting": list((getattr(item, "workflow", None) or {}).get("deterministicRouting") or []),
            "version": item.version,
            "status": item.status,
            "enabled": bool(item.enabled),
        }

    @staticmethod
    def _full(item: Any) -> dict[str, Any]:
        result = SkillCatalogCache._summary(item)
        result["content"] = str(item.content or "")
        return result

    async def invalidate(self) -> None:
        if not self.redis:
            return
        try:
            await self.redis.delete(self.catalog_key)
        except Exception:
            return

    async def load(self, db: AsyncSession) -> list[dict[str, Any]]:
        if self.redis:
            try:
                cached = await self.redis.get(self.catalog_key)
                if cached:
                    value = json.loads(cached)
                    if isinstance(value, list):
                        return value
            except Exception:
                pass
        from .db import Skill

        result = await db.execute(
            select(Skill)
            .where(Skill.scope == "system", Skill.enabled.is_(True), Skill.status == "PUBLISHED")
            .order_by(Skill.skill_id, Skill.version.desc())
        )
        items = list(result.scalars().all())
        catalog = [self._summary(item) for item in items]
        if self.redis:
            try:
                pipe = self.redis.pipeline()
                pipe.setex(self.catalog_key, SKILL_CACHE_TTL_SECONDS, json.dumps(catalog, ensure_ascii=False))
                for item in items:
                    pipe.setex(
                        f"{self.namespace}:skill:system:{item.skill_id}:v{item.version}",
                        SKILL_CACHE_TTL_SECONDS,
                        json.dumps(self._full(item), ensure_ascii=False),
                    )
                await pipe.execute()
            except Exception:
                pass
        return catalog


def normalized_router_mode(value: object) -> str:
    mode = str(value or "llm").strip().lower()
    return mode if mode in LLM_ROUTER_MODES else "llm"


def valid_llm_route(result: object, catalog: list[dict[str, Any]]) -> tuple[bool, str]:
    if not isinstance(result, dict):
        return False, "invalid_output"
    skill_id = result.get("skillId")
    if not isinstance(skill_id, str) or not skill_id.strip():
        return False, "missing_skill_id"
    if float(result.get("confidence", 0.0)) < LLM_ROUTER_MIN_CONFIDENCE:
        return False, "low_confidence"
    if not any(item.get("skillId") == skill_id.strip() for item in catalog):
        return False, "skill_not_published"
    return True, "needs_clarification" if bool(result.get("needsClarification", False)) else "ok"


@dataclass(frozen=True)
class DomainRecall:
    domains: list[str]
    candidates: list[dict[str, Any]]
    scores: dict[str, float]


def _normalized_text(value: object) -> str:
    return " ".join(str(value or "").lower().split())


def _phrase_score(message: str, phrases: list[object]) -> float:
    """Score configured phrases without turning the Skill ID into a keyword."""

    text = _normalized_text(message)
    score = 0.0
    for phrase in phrases:
        normalized = _normalized_text(phrase)
        if not normalized:
            continue
        if normalized in text:
            score += 1.0
            continue
        words = [word for word in re.findall(r"[\w-]+", normalized) if len(word) > 1]
        if len(words) >= 2:
            matched = sum(word in text for word in words)
            if matched / len(words) >= 0.7:
                score += 0.5
    return score


def _skill_domain(item: dict[str, Any]) -> str:
    return str(item.get("domain") or "general").strip() or "general"


def recall_skill_candidates(
    question: str,
    catalog: list[dict[str, Any]],
    context: dict[str, Any] | None = None,
) -> DomainRecall:
    """Recall one business domain from configured phrases, then its Skills."""

    context = context or {}
    messages: list[tuple[str, float]] = [(str(question or ""), 1.0)]
    prior_users = [
        str(item.get("content") or "")
        for item in reversed(list(context.get("recentMessages") or []))
        if isinstance(item, dict) and item.get("role") == "user" and str(item.get("content") or "").strip()
    ]
    messages.extend((message, DOMAIN_HISTORY_WEIGHTS[index]) for index, message in enumerate(prior_users[: len(DOMAIN_HISTORY_WEIGHTS)]))

    scores: dict[str, float] = {}
    for item in catalog:
        phrases = list(item.get("aliases", []) or []) + list(item.get("positiveExamples", []) or [])
        skill_score = sum(_phrase_score(message, phrases) * weight for message, weight in messages)
        if skill_score > 0:
            domain = _skill_domain(item)
            scores[domain] = scores.get(domain, 0.0) + skill_score

    active_domain = str(context.get("activeDomain") or "").strip()
    if active_domain and scores:
        scores[active_domain] = scores.get(active_domain, 0.0) + 0.10
    ranked = sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))
    if not ranked:
        return DomainRecall([], [], {})
    selected_domain = ranked[0][0]
    candidates = sorted(
        (item for item in catalog if _skill_domain(item) == selected_domain),
        key=lambda item: str(item.get("skillId") or ""),
    )
    return DomainRecall([selected_domain], candidates, {domain: round(score, 3) for domain, score in ranked})


def add_keyword_skill_candidate(
    recall: DomainRecall,
    catalog: list[dict[str, Any]],
    keyword_skill_id: object,
) -> DomainRecall:
    """Add the legacy resolver result as a published, LLM-selectable candidate.

    The database domain recall remains the primary candidate source. This
    compatibility anchor protects established intents while their Skill aliases
    are being configured, and never selects a route by itself.
    """

    skill_id = str(keyword_skill_id or "").strip()
    keyword_skill = next((item for item in catalog if item.get("skillId") == skill_id), None)
    if not keyword_skill or any(item.get("skillId") == skill_id for item in recall.candidates):
        return recall
    domain = _skill_domain(keyword_skill)
    domains = [*recall.domains]
    if domain not in domains:
        domains.append(domain)
    return DomainRecall(domains, [*recall.candidates, keyword_skill], recall.scores)


def configured_knowledge_fallback(catalog: list[dict[str, Any]], configured_skill_id: object) -> dict[str, Any] | None:
    """Return the published catalog entry selected as the global KB fallback."""

    skill_id = str(configured_skill_id or "").strip()
    item = next((candidate for candidate in catalog if candidate.get("skillId") == skill_id), None)
    if item and set(item.get("allowedTools") or []) == {"knowledge.search"}:
        return item
    return None


def route_context_from_history(history: list[Any], catalog: list[dict[str, Any]], max_messages: int = 5) -> dict[str, Any]:
    """Build bounded routing context from persisted events, excluding tool data."""
    from .skills import canonical_skill_id

    messages: list[dict[str, str]] = []
    active_skill_id = ""
    published_ids = {str(item.get("skillId") or "") for item in catalog}
    for event in history:
        event_type = getattr(event, "event_type", "")
        payload = getattr(event, "event_json", {}) or {}
        if event_type in {"user.message", "assistant.message"}:
            content = str(payload.get("content") or "").strip()
            if content:
                messages.append({"role": "user" if event_type == "user.message" else "assistant", "content": content[-1200:]})
        elif event_type == "skill.route":
            recorded_id = str(payload.get("skillId") or active_skill_id).strip()
            # Legacy aliases may still be valid published workflow IDs.
            active_skill_id = recorded_id if recorded_id in published_ids else canonical_skill_id(recorded_id)
    messages = messages[-max_messages:]
    active_domain = ""
    if active_skill_id:
        active_domain = str(next((item.get("domain") for item in catalog if item.get("skillId") == active_skill_id), "") or "")
    return {"activeSkillId": active_skill_id or None, "activeDomain": active_domain or None, "recentMessages": messages}

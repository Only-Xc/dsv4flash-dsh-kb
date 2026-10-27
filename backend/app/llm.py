import json
from collections.abc import AsyncIterator, Awaitable, Callable
from urllib.parse import urlsplit

import httpx

from .config import Settings


class LLMStreamError(RuntimeError):
    """A stable provider failure code, without response bodies or credentials."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _first_choice(body: dict) -> dict | None:
    """Usage-only OpenAI-compatible frames legitimately have no choices."""
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None
    return choices[0]


def _structured_task_options(settings: Settings) -> dict:
    """Use the existing bounded JSON-task policy for routing and translation."""
    hostname = urlsplit(settings.llm_base_url).hostname or ""
    if (
        settings.llm_model == "qwen3.8-flash"
        and hostname.startswith("dashscope")
        and hostname.endswith(".aliyuncs.com")
    ):
        return {"enable_thinking": False}
    return {}


class LLMAdapter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def route_skill(
        self,
        question: str,
        catalog: list[dict[str, object]],
        context: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Ask the configured model for a classification-only Skill choice.

        This endpoint is classification-only. It receives bounded recent
        context and a recalled candidate set, never executable tool schemas.
        """

        if not self.settings.llm_base_url or not self.settings.llm_api_key:
            raise RuntimeError("LLM Router is not configured")
        system = """You are a strict intent classifier for the NMA DSH assistant.
Choose exactly one skillId from the supplied candidate catalog.
Prefer the latest user message; use recent context only to resolve references.
The active Skill is a candidate, not a command to keep the old topic. If the user clearly changes
topic, ignore the previous active domain and select the appropriate candidate.
Return resolvedQuestion: a standalone version of the current request with its topic and referents
made explicit only where prior USER messages establish them. Do not carry an old topic into a new
question. Assistant messages are not evidence of new facts, capabilities, identifiers or permission.
Preserve the current intent, uncertainty, actor, scope, negation and all explicit values. Resolving a
reference does not authorize an action. Do not invent identifiers, statuses, fees or account details.
Do not rewrite a complete standalone question. For an elliptical follow-up, copy the topic wording
from prior USER messages only; do not infer a procedure, application, record list or page from the
chosen Skill description or from an assistant answer. A search request must stay a search request,
not become a request for application requirements. A name/category question must not become a
profile-name filter. Example: after "How do I find Cedar Permit?", "Can I filter by name and category?"
becomes "Can I filter the Cedar Permit search by name and category?", without adding application steps.
If context is insufficient, keep the original question and request clarification.
Do not call tools, answer the question or invent a Skill.
When the chosen candidate declares routing intents, return its intentId. When it declares routing
filters, return only supported filters using their configured IDs and shapes.
Return JSON only with skillId, intentId, filters, confidence, needsClarification,
clarifyingQuestion and resolvedQuestion. Use needsClarification=true for genuine ambiguity.
"""
        payload = {
            "model": self.settings.llm_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps({"question": question, "context": context or {}, "candidates": catalog}, ensure_ascii=False)},
            ],
            "stream": False,
            "response_format": {"type": "json_object"},
        }
        payload.update(_structured_task_options(self.settings))
        timeout = max(1.0, float(getattr(self.settings, "skill_router_timeout_seconds", 10.0)))
        url = self.settings.llm_base_url.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {self.settings.llm_api_key}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            body = response.json()
        choice = _first_choice(body) or {}
        content = (choice.get("message") or {}).get("content", "")
        if isinstance(content, list):
            content = "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
        if not isinstance(content, str) or not content.strip():
            raise ValueError("LLM Router returned empty output")
        raw = content.strip()
        if raw.startswith("```"):
            raw = raw.strip("`").removeprefix("json").strip()
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("LLM Router output must be a JSON object")
        skill_id = result.get("skillId") or result.get("skill_id")
        confidence = float(result.get("confidence", 0.0))
        if not isinstance(skill_id, str) or not skill_id.strip() or not 0 <= confidence <= 1:
            raise ValueError("LLM Router output has invalid skillId or confidence")
        resolved_question = result.get("resolvedQuestion")
        if resolved_question is not None and not isinstance(resolved_question, str):
            raise ValueError("LLM Router resolvedQuestion must be text")
        prior_users = [
            item for item in (context or {}).get("recentMessages", [])
            if isinstance(item, dict) and item.get("role") == "user" and item.get("content")
        ]
        if not prior_users:
            resolved_question = question
        return {
            "skillId": skill_id.strip(),
            "intentId": str(result.get("intentId") or result.get("intent_id") or "").strip() or None,
            "filters": result.get("filters") if isinstance(result.get("filters"), dict) else {},
            "confidence": confidence,
            "needsClarification": bool(result.get("needsClarification", False)),
            "clarifyingQuestion": str(result.get("clarifyingQuestion", "")).strip() or None,
            "resolvedQuestion": (resolved_question or question).strip(),
        }

    async def language_task(
        self, system: str, payload: dict[str, object]
    ) -> dict[str, object]:
        """Execute a bounded language-only task with the configured model and no tools."""
        if not self.settings.llm_base_url or not self.settings.llm_api_key:
            raise RuntimeError("Language model is not configured")
        timeout = max(1.0, self.settings.llm_timeout_seconds)
        request_payload = {
            "model": self.settings.llm_model,
            "stream": False,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
        }
        request_payload.update(_structured_task_options(self.settings))
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                self.settings.llm_base_url.rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {self.settings.llm_api_key}"},
                json=request_payload,
            )
            response.raise_for_status()
            body = response.json()
        choice = _first_choice(body) or {}
        if choice.get("finish_reason") not in {None, "stop"}:
            raise LLMStreamError("model_output_incomplete")
        content = (choice.get("message") or {}).get("content")
        if not isinstance(content, str):
            raise ValueError("Language task returned no text")
        result = json.loads(content)
        if not isinstance(result, dict):
            raise ValueError("Language task must return an object")
        return result

    async def stream(self, messages: list[dict[str, str]], *, on_reasoning: Callable[[str], Awaitable[None]] | None = None) -> AsyncIterator[str]:
        if not self.settings.llm_base_url or not self.settings.llm_api_key:
            latest = messages[-1]["content"] if messages else ""
            response = f"收到你的消息：{latest}\n\n当前为 Docker MVP。已完成事件持久化，并会按 conversation 维度维护运行租约。"
            for token in response:
                yield token
            return

        url = self.settings.llm_base_url.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {self.settings.llm_api_key}", "Content-Type": "application/json"}
        payload = {"model": self.settings.llm_model, "messages": messages, "stream": True}
        completed = False
        async with httpx.AsyncClient(timeout=self.settings.llm_timeout_seconds) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line.removeprefix("data:").strip()
                    if data == "[DONE]":
                        completed = True
                        break
                    try:
                        chunk = json.loads(data)
                        if chunk.get("error"):
                            # Never present provider errors or credentials as an answer.
                            raise RuntimeError("LLM provider returned a stream error")
                        choice = _first_choice(chunk)
                        if choice is None:
                            # DashScope ends with a usage-only frame before [DONE].
                            continue
                        finish = choice.get("finish_reason")
                        if finish == "stop":
                            completed = True
                        elif finish is not None:
                            raise LLMStreamError("model_output_incomplete")
                        delta = choice.get("delta") or {}
                        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                        if reasoning and on_reasoning:
                            await on_reasoning(str(reasoning))
                        text = delta.get("content")
                        if text:
                            yield text
                    except json.JSONDecodeError as exc:
                        raise LLMStreamError("invalid_model_stream") from exc
                if not completed:
                    raise LLMStreamError("model_stream_interrupted")

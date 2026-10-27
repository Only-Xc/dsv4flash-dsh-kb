"""Portal-independent, fail-closed streaming of public answer blocks.

The caller owns authorization, evidence binding and language validation. Raw
model tokens are never public events. Only complete, accepted blocks are sent;
there is no character pacing or simulated typing delay. A terminal message must
contain exactly the published text, so a later retry cannot retract a disclosure.
"""

from __future__ import annotations

import inspect
import json
import re
import time
from collections.abc import AsyncIterable, Awaitable, Callable, Iterable

from .language_messages import language_message


Validator = Callable[[str], bool | Awaitable[bool]]
Publisher = Callable[[dict], Awaitable[object]]


class AnswerStreamRejected(ValueError):
    """Unpublished text failed validation; already published text is immutable."""


class AnswerStream:
    protocol_version = "verified-blocks/1"

    def __init__(self, publish: Publisher, *, max_block_chars: int = 16000):
        if max_block_chars < 1:
            raise ValueError("max_block_chars must be positive")
        self._publish = publish
        self.max_block_chars = max_block_chars
        self.content = ""
        self.status = "complete"
        self.block_count = 0
        self.last_seq = 0
        self.started_at = time.perf_counter()
        self.block_receipts: list[dict] = []
        self.attempts: list[dict] = []
        self.failure_reason: str | None = None

    def discard_private_draft(self) -> None:
        """Only the owner of an unpublished working stream may discard a draft."""
        self.content = ""
        self.block_count = 0
        self.last_seq = 0
        self.block_receipts.clear()
        self.status = "complete"
        self.failure_reason = None

    async def _send(self, text: str, *, mode: str) -> None:
        if not text:
            return
        # Commit the prefix only after successful transport publication. There
        # is deliberately no model text or source evidence in this metadata.
        receipt = await self._publish({
            "content": text,
            "streamVersion": self.protocol_version,
            "chunkIndex": self.block_count,
            "streamMode": mode,
        })
        if isinstance(receipt, dict) and isinstance(receipt.get("seq"), int):
            self.last_seq = max(self.last_seq, receipt["seq"])
        self.content += text
        self.block_receipts.append({
            "chunkIndex": self.block_count, "mode": mode,
            "elapsedMs": round((time.perf_counter() - self.started_at) * 1000, 1),
        })
        self.block_count += 1

    @staticmethod
    def public_blocks(text: str) -> Iterable[str]:
        """Lossless Markdown blocks, keeping fences and tables together.

        A record/list line is an atomic projection, not a substring of an
        identifier. Leading/trailing whitespace is retained for final parity.
        """
        pending = ""
        fence = False
        table = False
        for line in text.splitlines(keepends=True):
            stripped = line.lstrip()
            if stripped.startswith(("```", "~~~")):
                fence = not fence
            is_table = stripped.startswith("|")
            if pending and table and not is_table and not fence:
                yield pending
                pending = ""
            table = is_table
            pending += line
            if not fence and not table and (not line.strip() or stripped.startswith(("- ", "* "))):
                yield pending
                pending = ""
        if pending:
            yield pending

    async def verified(self, text: str) -> str:
        """Publish an already validated projection without rewriting its prefix."""
        if not text.startswith(self.content):
            raise AnswerStreamRejected("final answer would rewrite a published prefix")
        for block in self.public_blocks(text[len(self.content):]):
            await self._send(block, mode="verified_projection")
        return self.content

    async def generate(
        self, tokens: AsyncIterable[str], *, validate: Validator,
        sanitize: Callable[[str], str] = lambda text: text,
    ) -> str:
        """Generate and validate complete paragraphs before the model finishes.

        Validation sees the cumulative public candidate so qualifiers/numeric
        coverage cannot be checked against only an isolated token. A failed
        candidate is kept private until a later block makes it valid, with a
        hard buffer limit. End-of-stream failure is never a successful answer.
        Callers may retry only when ``content`` is still empty; after publication
        they must preserve this prefix and report an incomplete response.
        """
        pending = ""
        async for token in tokens:
            if not isinstance(token, str):
                raise AnswerStreamRejected("model fragment must be text")
            pending += token
            if len(pending) > self.max_block_chars:
                raise AnswerStreamRejected("unvalidated block exceeded limit")
            # Do not split an unfinished Markdown link, JSON object or fenced
            # payload at arbitrary token sizes. Complete paragraphs are the
            # earliest safe presentation boundary, not per-character output.
            boundaries = list(re.finditer(r"\n[ \t]*\n", pending))
            if not boundaries:
                continue
            boundary = boundaries[-1].end()
            raw = pending[:boundary]
            if raw.count("```") % 2 or raw.count("~~~") % 2:
                continue
            candidate = sanitize(self.content + raw)
            if not candidate.startswith(self.content):
                raise AnswerStreamRejected("sanitization would rewrite a published prefix")
            accepted = public_candidate(candidate) and validate(candidate)
            if inspect.isawaitable(accepted):
                accepted = await accepted
            if accepted is not True:
                continue
            await self._send(candidate[len(self.content):], mode="guarded_generation")
            pending = pending[boundary:]
        if pending:
            candidate = sanitize(self.content + pending)
            if not candidate.startswith(self.content):
                raise AnswerStreamRejected("sanitization would rewrite a published prefix")
            accepted = public_candidate(candidate) and validate(candidate)
            if inspect.isawaitable(accepted):
                accepted = await accepted
            if accepted is not True:
                raise AnswerStreamRejected("public_content_rejected")
            await self._send(candidate[len(self.content):], mode="guarded_generation")
        if not self.content.strip():
            raise AnswerStreamRejected("empty generated answer")
        return self.content


def public_candidate(text: str) -> bool:
    """Reject serialized tool/secret frames even when a portal guard is lax."""
    if re.search(r"dsml|<\s*(?:tool_calls?|function_calls?|invoke)\b|"
                 r"\b(?:api[_ -]?key|access[_ -]?token|authorization)[\"']?\s*[:=]|"
                 r"[\"']password[\"']\s*:|"
                 r"\bpassword\s*=|"
                 r"\bpassword\s*:\s*[\"'][^\"'\n]+[\"']|"
                 r"(?m:^[ \t]*(?:[-*] )?password\s*:\s*[^\s]{8,}[ \t]*$)|"
                 r"[\"'](?:tool|toolName|tool_calls?|function_calls?)[\"']\s*:",
                 text, re.I):
        return False
    # Unbalanced or unfinished fenced code blocks mean the model stream ended
    # mid-structure; such text is not a publishable Markdown answer.
    body = text
    if body.count("```") % 2 or body.count("~~~") % 2:
        return False
    if re.search(r"\[[^\]\n]*\]\([^)\n]*$", body) or re.search(r"\[[^\]\n]*$", body):
        return False
    candidate = text.strip()
    if candidate.startswith("JSON\n"):
        candidate = candidate[5:]
    if candidate.startswith("```json") and candidate.endswith("```"):
        candidate = candidate[7:-3].strip()
    try:
        payload = json.loads(candidate)
    except (ValueError, TypeError):
        return True
    return not (isinstance(payload, dict) and
                {"tool", "toolName", "name"}.intersection(payload) and
                {"args", "arguments", "parameters"}.intersection(payload))


def incomplete_stream_notice(language: str) -> str:
    """A failure receipt, never an invented business conclusion."""
    if language in {"en", "ar", "ur", "ml", "tl"}:
        return "\n\n" + language_message(language, "partial")
    return {
        "ar": "\n\nلم يكتمل الرد لأن الجزء المتبقي لم يجتز التحقق أو تعذر توليده. النص المعروض جزئي؛ يرجى إعادة المحاولة.",
        "zh": "\n\n回答未完成：剩余内容未通过校验或生成中断。当前显示的是部分回答，请重试。",
    }.get(language, "\n\nThe answer is incomplete because the remaining content could not be validated or generated. The displayed response is partial; please retry.")


def cancelled_stream_notice(language: str) -> str:
    if language in {"ur", "ml", "tl"}:
        return "\n\n" + language_message(language, "cancelled")
    return {"ar": "\n\nتم إيقاف الرد؛ النص المعروض جزئي.",
            "zh": "\n\n已停止输出，当前内容为部分回答。"}.get(
        language, "\n\nThe response was stopped; the displayed answer is partial.")

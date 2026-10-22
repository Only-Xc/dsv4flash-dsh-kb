"""Transport fixtures are not live business acceptance data."""
import asyncio

import pytest

from app.answer_stream import AnswerStream, AnswerStreamRejected, incomplete_stream_notice


def test_safe_paragraph_is_sent_before_generation_finishes():
    async def run():
        events = []
        async def publish(value):
            events.append(value)
        stream = AnswerStream(publish)
        async def tokens():
            yield "First verified paragraph.\n"
            assert not events
            yield "\n"
            assert events[0]["content"] == "First verified paragraph.\n\n"
            yield "Second verified paragraph."
        result = await stream.generate(tokens(), validate=lambda value: True)
        assert result == "".join(event["content"] for event in events)
        assert [event["chunkIndex"] for event in events] == [0, 1]
        assert all(event["streamVersion"] == "verified-blocks/1" for event in events)
    asyncio.run(run())


def test_rejected_fragment_never_reaches_client_and_prefix_cannot_be_retried():
    async def run():
        events = []
        async def publish(value): events.append(value)
        stream = AnswerStream(publish)
        async def tokens():
            yield "Safe.\n\n"
            yield "PRIVATE_SECRET\n\n"
        with pytest.raises(AnswerStreamRejected):
            await stream.generate(tokens(), validate=lambda text: "PRIVATE_SECRET" not in text)
        assert stream.content == "Safe.\n\n"
        assert "PRIVATE_SECRET" not in str(events)
        with pytest.raises(AnswerStreamRejected):
            await stream.verified("A replacement response.")
        await stream.verified(stream.content + incomplete_stream_notice("en"))
        assert "partial" in stream.content
    asyncio.run(run())


@pytest.mark.parametrize("text", [
    "English heading\n\n- Record: X-123\n- Count: 3\n\nScope: current view.",
    "النتائج\n\n- رقم الطلب: X-123\n- العدد: ٣\n\nنطاق الحساب الحالي.",
    "| Name | Value |\n| --- | --- |\n| A | 1 |\n\nNext paragraph.",
    "```text\nline\n\nline\n```\n\nEnd.",
])
def test_verified_projection_is_lossless_and_retains_markdown(text):
    async def run():
        events = []
        async def publish(value): events.append(value)
        stream = AnswerStream(publish)
        assert await stream.verified(text) == text
        assert "".join(event["content"] for event in events) == text
        assert len(events) >= 2
        if text.startswith("|"):
            assert events[0]["content"].count("|") == 9
    asyncio.run(run())


def test_sanitizer_cannot_rewrite_an_already_emitted_prefix():
    async def run():
        async def publish(value): pass
        stream = AnswerStream(publish)
        async def tokens():
            yield "Safe.\n\n"
            yield "tail"
        with pytest.raises(AnswerStreamRejected):
            await stream.generate(tokens(), validate=lambda value: True,
                sanitize=lambda text: text.replace("Safe", "Changed") if "tail" in text else text)
        assert stream.content == "Safe.\n\n"
    asyncio.run(run())


def test_cancel_propagates_without_flushing_unvalidated_buffer():
    async def run():
        events = []
        async def publish(value): events.append(value)
        stream = AnswerStream(publish)
        async def tokens():
            yield "Unfinished secret"
            raise asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await stream.generate(tokens(), validate=lambda value: True)
        assert not events
    asyncio.run(run())


def test_guard_can_hold_incomplete_coverage_until_a_later_block():
    async def run():
        events = []
        async def publish(value): events.append(value)
        stream = AnswerStream(publish)
        async def tokens():
            yield "Subtotal.\n\n"
            assert not events
            yield "Required total.\n\n"
            assert len(events) == 1
        result = await stream.generate(tokens(), validate=lambda text: "Required total" in text)
        assert result == "Subtotal.\n\nRequired total.\n\n"
    asyncio.run(run())


def test_unbounded_or_empty_private_output_fails_closed():
    async def run():
        async def publish(value): raise AssertionError("must not publish")
        for text in ("", "123456"):
            stream = AnswerStream(publish, max_block_chars=5)
            async def tokens(): yield text
            with pytest.raises(AnswerStreamRejected):
                await stream.generate(tokens(), validate=lambda value: False)
    asyncio.run(run())


@pytest.mark.parametrize("text", [
    '{"tool":"internal.read","args":{"id":1}}',
    '<tool_calls><invoke name="internal.read"/></tool_calls>',
    'API key: PRIVATE_SECRET',
    'authorization=Bearer PRIVATE_SECRET',
    '{"password":"PRIVATE_SECRET"}',
    '{"access_token":"PRIVATE_SECRET"}',
    '{"tool":"internal.read",\n\n',
])
def test_internal_payload_is_blocked_before_portal_validator(text):
    async def run():
        async def publish(value): raise AssertionError("private output published")
        stream = AnswerStream(publish)
        async def tokens():
            yield text[:6]
            yield text[6:]
        with pytest.raises(AnswerStreamRejected):
            await stream.generate(tokens(), validate=lambda value: True)
    asyncio.run(run())


@pytest.mark.parametrize("text", [
    'See [the portal](https://example.com',
    'See [the portal',
])
def test_unfinished_markdown_structure_is_not_published(text):
    async def run():
        async def publish(value): raise AssertionError("unfinished markdown published")
        stream = AnswerStream(publish)
        async def tokens():
            yield text[:5]
            yield text[5:]
        with pytest.raises(AnswerStreamRejected):
            await stream.generate(tokens(), validate=lambda value: True)
    asyncio.run(run())


def test_unclosed_code_fence_at_end_of_stream_is_rejected():
    async def run():
        events = []
        async def publish(value): events.append(value)
        stream = AnswerStream(publish)
        async def tokens():
            # The completed paragraph is publishable; the trailing fence is not.
            yield "Conclusion first.\n\n"
            yield "```python\nprint(\"hi\")"
        with pytest.raises(AnswerStreamRejected):
            await stream.generate(tokens(), validate=lambda value: True)
        assert events == [{"content": "Conclusion first.\n\n", "streamVersion": "verified-blocks/1",
                           "chunkIndex": 0, "streamMode": "guarded_generation"}]
    asyncio.run(run())


def test_transport_receipt_keeps_cancel_history_sequence_after_ephemeral_chunks():
    async def run():
        async def publish(value): return {"seq": 50 + value["chunkIndex"]}
        stream = AnswerStream(publish)
        await stream.verified("First.\n\nSecond.")
        assert stream.last_seq == 51
    asyncio.run(run())

"""Answer style contract for customer-facing generation."""
import asyncio

from app.customer_answer_stream import ANSWER_FORMAT_SYSTEM_PROMPT, customer_generated_answer


class FakeStreamLLM:
    def __init__(self, answer: str):
        self.answer = answer
        self.prompts: list[list[dict]] = []

    def stream(self, messages, *, on_reasoning=None):
        self.prompts.append(messages)

        async def tokens():
            for index in range(0, len(self.answer), 6):
                yield self.answer[index:index + 6]
        return tokens()


def run_customer_answer(answer: str, language: str = "en"):
    async def run():
        published: list[str] = []

        async def publish(event):
            published.append(event["content"])

        from app.answer_stream import AnswerStream
        stream = AnswerStream(publish)
        llm = FakeStreamLLM(answer)
        content, _, status = await customer_generated_answer(stream, llm, [], language)
        return content, status, llm.prompts, published
    return asyncio.run(run())


def test_format_rules_are_included_in_generation_prompt():
    _, status, prompts, _ = run_customer_answer("Verified conclusion.\n\nNext step.")
    assert status == "complete"
    system_messages = [
        message["content"] for message in prompts[0] if message["role"] == "system"
    ]
    assert any(ANSWER_FORMAT_SYSTEM_PROMPT in message for message in system_messages)


def test_business_fields_survive_style_constraints():
    answer = (
        "Application RE-2-302-941808 is Rejected.\n\n"
        "- Service: Media Materials Entry\n"
        "- Date: 21/10/2026\n"
        "- Fee: AED 16,000.00"
    )
    content, status, _, published = run_customer_answer(answer)
    assert status == "complete"
    assert content == answer
    assert "".join(published) == answer
    for field in ("RE-2-302-941808", "Rejected", "AED 16,000.00"):
        assert field in content


def test_multitopic_answer_uses_markdown_headings_and_lists():
    answer = (
        "## Customer Portal\n\n"
        "- UAE PASS login\n"
        "- Email and password login\n\n"
        "## Admin Portal\n\n"
        "Sign-in flow exists, but exact login methods are not specified."
    )
    content, status, _, _ = run_customer_answer(answer)
    assert status == "complete"
    assert "## Customer Portal" in content
    assert "- UAE PASS login" in content


def test_arabic_answer_stays_in_arabic():
    answer = (
        "المصادقة متاحة عبر البوابة.\n\n"
        "- بوابة العملاء\n"
        "- بوابة المسؤول"
    )
    content, status, _, _ = run_customer_answer(answer, language="ar")
    assert status == "complete"
    assert content == answer

"""Answer style contract for customer-facing generation."""
import asyncio

from app.customer_answer_stream import ANSWER_FORMAT_SYSTEM_PROMPT, customer_generated_answer


def test_login_explanation_with_password_label_is_not_a_secret():
    from app.answer_stream import public_candidate

    for text in (
        "Email and password: enter your registered email and password.",
        "**Email and password:** enter your registered details.",
        "- Email and password — choose Login.",
        "Email and password: available",
        "Email and password: Instructions",
    ):
        assert public_candidate(text)
    assert not public_candidate('password: PRIVATE_SECRET')
    assert not public_candidate('{"password":"PRIVATE_SECRET"}')
    assert not public_candidate('- Password: PRIVATE_SECRET')
    assert not public_candidate('Your password: "PRIVATE_SECRET"')


def test_private_generation_recovers_without_publishing_partial_draft():
    import asyncio
    from app.answer_stream import AnswerStream

    class Model:
        calls = 0

        async def stream(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                yield "First unfinished draft.\n\n"
                raise TimeoutError("private provider information")
            yield "UAE PASS or email and password."

    async def run():
        events = []
        async def publish(value):
            events.append(value)
        stream = AnswerStream(publish)
        model = Model()
        text, _, status = await customer_generated_answer(
            stream, model, [], "en", private_draft=True,
        )
        assert status == "complete"
        assert text == "UAE PASS or email and password."
        assert model.calls == 2
        assert stream.attempts[0]["failure"] == "generation_timeout"
        assert "private provider information" not in str(stream.attempts)

    asyncio.run(run())


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

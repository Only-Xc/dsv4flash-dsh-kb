"""Isolated language-boundary tests; fixtures do not prove native-language quality."""

import asyncio

import pytest
from app.language_bridge import (
    LanguageBridge,
    LanguageConversionAgent,
    LanguageDetector,
)
from app.language_messages import language_message


class ScriptedModel:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def language_task(self, system, payload):
        self.calls.append((system, payload))
        if payload.get("mode") == "review":
            return {
                "source_meaning": "The supplied source proposition.",
                "candidate_meaning": "The supplied source proposition.",
                "verdict": "equivalent",
                "differences": [],
            }
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def decision(source, target=None, **extra):
    return {"detected_language": source, "target_language": target or source, **extra}


def translation(text, **extra):
    return {"text": text, **extra}


def test_original_identifiers_links_and_code_reach_translation_unchanged():
    async def run():
        source = (
            "Check APP-2001, 25.50 and [source](/docs/fees#rates). Use `no_change`."
        )
        candidate = "Suriin ang APP-2001, 25.50 at [sanggunian](/docs/fees#rates). Gamitin ang `no_change`."
        model = ScriptedModel(translation(candidate))
        result = await LanguageConversionAgent(model).convert(
            source, "en", "tl", mode="output"
        )
        assert result == candidate
        assert model.calls[0][1]["text"] == source
        assert model.calls[1][1]["source"] == source
        assert "protected_spans" not in model.calls[0][1]

    asyncio.run(run())


def test_identifier_language_is_decided_by_the_detector_model():
    async def run():
        model = ScriptedModel(decision("ur"))
        detector = LanguageDetector(model)
        result = await detector.identify("APP-2001", "ur", [])
        assert result.target_language == "ur"
        assert model.calls[0][1]["text"] == "APP-2001"
        assert model.calls[0][1]["previous_language"] == "ur"

    asyncio.run(run())


@pytest.mark.parametrize("source,target,text", [
    ("ar", "en", "لا تلغ الطلب"),
    ("en", "ar", "Do not cancel the application"),
])
def test_explicit_native_language_switch_uses_consistent_working_input(source, target, text):
    async def run():
        responses = [decision(source, target)]
        if source != "en":
            responses.append({"text": "Do not cancel the application"})
        model = ScriptedModel(*responses)
        turn = await LanguageBridge(model).prepare(text)
        assert turn.requires_bridge
        assert turn.working_language == "en"
        assert turn.working_text == "Do not cancel the application"

    asyncio.run(run())


@pytest.mark.parametrize("language", ["en", "ar"])
def test_native_answers_are_not_translated(language):
    async def run():
        model = ScriptedModel(decision(language))
        bridge = LanguageBridge(model)
        turn = await bridge.prepare("A native-language question")
        assert not turn.failure
        assert not turn.requires_bridge
        assert (
            await bridge.render(turn, "Verified native answer")
            == "Verified native answer"
        )
        assert len(model.calls) == 1

    asyncio.run(run())


def test_input_output_conversion_and_request_local_reuse():
    async def run():
        model = ScriptedModel(
            decision("tl"),
            translation("Show my applications."),
            translation("Narito ang iyong mga aplikasyon."),
        )
        bridge = LanguageBridge(model)
        turn = await bridge.prepare("Ipakita ang aking mga aplikasyon.")
        assert turn.working_text == "Show my applications."
        assert turn.original_text == "Ipakita ang aking mga aplikasyon."
        assert turn.working_language == "en"
        answer = await bridge.render(turn, "Here are your applications.")
        assert answer == "Narito ang iyong mga aplikasyon."
        assert await bridge.render(turn, "Here are your applications.") == answer
        assert len(model.calls) == 5
        assert all("tools" not in payload for _, payload in model.calls)

    asyncio.run(run())


def test_explicit_target_language_does_not_retranslate_english_input():
    async def run():
        model = ScriptedModel(decision("en", "tl"), translation("Narito ang sagot."))
        bridge = LanguageBridge(model)
        turn = await bridge.prepare("Please reply in Tagalog.")
        assert turn.requires_bridge
        assert turn.working_text == turn.original_text
        assert await bridge.render(turn, "Here is the answer.") == "Narito ang sagot."
        assert len(model.calls) == 3

    asyncio.run(run())


@pytest.mark.parametrize(
    "response",
    [
        {"text": ""},
        {"text": "   "},
        {"text": 7},
        {
            "text": "Cancel it.",
            "unexpected": True,
        },
        RuntimeError("provider credential must remain private"),
    ],
)
def test_failed_input_is_visible_and_cannot_be_used_as_working_english(response):
    async def run():
        model = ScriptedModel(decision("tl"), response)
        bridge = LanguageBridge(model)
        turn = await bridge.prepare("Huwag kanselahin.")
        assert turn.failure
        receipt = await bridge.render(turn, "An internal English draft")
        assert receipt in {
            language_message("tl", "failure"),
            language_message("tl", "clarify"),
        }
        assert "credential" not in turn.failure
        assert "internal English" not in receipt

    asyncio.run(run())


def test_failed_output_never_returns_the_english_intermediate():
    async def run():
        model = ScriptedModel(decision("en", "tl"), RuntimeError("unavailable"))
        bridge = LanguageBridge(model)
        turn = await bridge.prepare("Answer in Tagalog.")
        assert await bridge.render(turn, "Private English draft") == language_message(
            "tl", "failure"
        )
        assert turn.failure == "output_translation_failed"

    asyncio.run(run())


def test_cancellation_propagates_across_language_boundary():
    async def run():
        model = ScriptedModel(asyncio.CancelledError())
        with pytest.raises(asyncio.CancelledError):
            await LanguageBridge(model).prepare("Show my applications.")

    asyncio.run(run())


@pytest.mark.parametrize("text", ["APP-2001", "2", "no", "yes", "okay"])
def test_short_references_follow_model_detection_and_review(text):
    async def run():
        model = ScriptedModel(decision("ur"), translation(text))
        turn = await LanguageBridge(model).prepare(text, previous_language="ur")
        assert turn.target_language == "ur"
        assert turn.working_text == text
        assert not turn.failure
        assert len(model.calls) == 3

    asyncio.run(run())


def test_profile_names_remain_visible_in_translation_and_review():
    async def run():
        model = ScriptedModel(translation("Piliin ang No Company."))
        text = await LanguageConversionAgent(model).convert(
            "Select No Company.",
            "en",
            "tl",
            mode="output",
        )
        assert text == "Piliin ang No Company."
        assert model.calls[0][1]["text"] == "Select No Company."
        assert model.calls[1][1]["source"] == "Select No Company."

    asyncio.run(run())


def test_rendered_json_and_context_are_passed_without_replacement():
    async def run():
        source = 'Result: {"owner":"Private Name","amount":25,"nested":{"status":"Completed"}}'
        candidate = source.replace("Result:", "Resulta:")
        model = ScriptedModel(translation(candidate))
        context = [{"role": "assistant", "content": source}]
        result = await LanguageConversionAgent(model).convert(
            source, "en", "tl", mode="output", context=context
        )
        assert result == candidate
        assert model.calls[0][1]["text"] == source
        assert model.calls[0][1]["context"] == context

    asyncio.run(run())


@pytest.mark.parametrize(
    "classification",
    [
        decision("und", "und"),
        decision("tl", needs_clarification=True),
    ],
)
def test_uncertain_or_unsupported_language_stops_before_input_conversion(
    classification,
):
    async def run():
        model = ScriptedModel(classification, classification)
        turn = await LanguageBridge(model).prepare("An uncertain request")
        assert turn.failure
        assert len(model.calls) == 2

    asyncio.run(run())

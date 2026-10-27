"""Semantic-review contracts and reproduced language-boundary regressions."""

import asyncio
from types import SimpleNamespace

import pytest
from app.language_bridge import (
    LanguageBridge,
    LanguageConversionAgent,
    LanguageDetector,
)
from app.language_contracts import LanguageBridgeError
from app.language_history import working_history
from app.language_messages import language_message
from app.language_prompts import PROMPT_VERSION


class ReviewModel:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def language_task(self, system, payload):
        self.calls.append((system, payload))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def translated(text):
    return {"text": text}


def reviewed(source="", candidate="", verdict="equivalent"):
    return {
        "source_meaning": source or "The original proposition.",
        "candidate_meaning": candidate or "The original proposition.",
        "verdict": verdict,
        "differences": []
        if verdict == "equivalent"
        else [
            {
                "source_quote": source,
                "candidate_quote": candidate,
                "explanation": "The candidate changes the source meaning.",
            }
        ],
    }


@pytest.mark.parametrize("subject,meaning", [("payment", "pay"), ("window", "close")])
def test_followup_review_gets_the_same_bounded_context_as_translation(subject, meaning):
    async def run():
        source = "ഞാൻ അത് വീണ്ടും അടയ്ക്കണോ?"
        candidate = f"Should I {meaning} it again?"
        history = [{"role": "user", "content": "older context"}] * 2 + [
            {"role": "assistant", "content": "x" * 2100},
            {"role": "user", "content": f"I have a problem with my {subject}."},
            {"role": "assistant", "content": "Please check its current status."},
        ]
        model = ReviewModel(
            translated(candidate), {"verdict": "equivalent"}, reviewed()
        )
        result = await LanguageConversionAgent(model).convert(
            source, "ml", "en", mode="input", context=history
        )
        assert result == candidate
        contexts = [payload["context"] for _, payload in model.calls]
        assert contexts[0] == contexts[1] == contexts[2]
        assert len(contexts[0]) == 4
        assert len(contexts[0][1]["content"]) == 2000
        assert model.calls[-1][1]["repair_feedback"]["failure"] == "invalid_language_review"
        assert len(history[2]["content"]) == 2100

    asyncio.run(run())


def test_semantic_repair_keeps_context_without_anchoring_reviewer_to_old_feedback():
    async def run():
        source, bad, good = (
            "ഞാൻ അത് വീണ്ടും അടയ്ക്കണോ?",
            "Should I close it again?",
            "Should I pay it again?",
        )
        context = [{"role": "user", "content": "My payment still appears unpaid."}]
        model = ReviewModel(
            translated(bad),
            reviewed("അടയ്ക്കണോ", "close", "different"),
            translated(good),
            reviewed(),
        )
        result = await LanguageConversionAgent(model).convert(
            source, "ml", "en", mode="input", context=context
        )
        assert result == good
        assert all(payload["context"] == context for _, payload in model.calls)
        assert model.calls[-1][1]["repair_feedback"] is None

    asyncio.run(run())


def test_review_cannot_quote_context_in_place_of_current_source():
    async def run():
        model = ReviewModel(
            translated("Should I pay it again?"),
            reviewed("previous payment", "pay", "different"),
        )
        with pytest.raises(LanguageBridgeError, match="ungrounded_language_review"):
            await LanguageConversionAgent(model).convert(
                "ഞാൻ അത് വീണ്ടും അടയ്ക്കണോ?",
                "ml",
                "en",
                mode="input",
                context=[{"role": "user", "content": "previous payment"}],
            )

    asyncio.run(run())


def test_domain_independent_review_repairs_a_changed_object_before_acceptance():
    async def run():
        source = "میری خریداری کی درخواست کی حیثیت دکھائیں۔"
        bad = "Approve my purchase order."
        corrected = "Show the status of my purchase request."
        model = ReviewModel(
            translated(bad),
            reviewed(source, bad, "different"),
            translated(corrected),
            reviewed(),
        )
        records = []
        result = await LanguageConversionAgent(model).convert(
            source, "ur", "en", mode="input", review_records=records
        )
        assert result == corrected
        assert len(model.calls) == 4
        assert model.calls[2][1]["repair_feedback"]["failure"] == "semantic_mismatch"
        assert model.calls[3][1]["repair_feedback"] is None
        assert model.calls[1][1]["mode"] == "review"
        assert "meaning_preserved" not in model.calls[1][1]
        assert "glossary" not in model.calls[0][1]
        assert records[-1]["review"]["verdict"] == "equivalent"
        assert "Source language: Urdu" in model.calls[0][0]
        assert "Target language: English" in model.calls[0][0]

    asyncio.run(run())


@pytest.mark.parametrize(
    "source,bad,corrected",
    [
        (
            "മറ്റൊരു കമ്പനിയുടെ അപേക്ഷ പരിശോധിക്കുക.",
            "Review the appeal of another company.",
            "Check another company's application.",
        ),
        (
            "کتنی ابھی کارروائی میں ہیں؟",
            "How many are under review?",
            "How many are still in progress?",
        ),
        (
            "ഈ അപേക്ഷ റദ്ദാക്കുക.",
            "Reject this application.",
            "Cancel this application.",
        ),
    ],
)
def test_reproduced_object_and_status_changes_get_one_reviewed_repair(
    source, bad, corrected
):
    async def run():
        language = "ml" if "അപേക്ഷ" in source else "ur"
        model = ReviewModel(
            translated(bad),
            reviewed(source, bad, "different"),
            translated(corrected),
            reviewed(),
        )
        assert (
            await LanguageConversionAgent(model).convert(
                source, language, "en", mode="input"
            )
            == corrected
        )

    asyncio.run(run())


def test_two_semantically_wrong_attempts_fail_closed():
    async def run():
        source = "Ipakita ang aking mga aplikasyon."
        bad = "Show my appeals."
        model = ReviewModel(
            {"detected_language": "tl", "target_language": "tl"},
            translated(bad),
            reviewed(source, bad, "different"),
            translated(bad),
            reviewed(source, bad, "different"),
        )
        bridge = LanguageBridge(model)
        turn = await bridge.prepare(source)
        assert turn.failure == "semantic_mismatch"
        assert turn.working_text == source
        assert len(turn.input_reviews) == 2
        assert await bridge.render(turn, "Private business draft") == language_message(
            "tl", "failure"
        )
        assert len(model.calls) == 5

    asyncio.run(run())


@pytest.mark.parametrize(
    "invalid", ["```text\nHindi pa kumpleto", 'password: "PRIVATE_SECRET"']
)
def test_output_public_validation_repairs_even_when_semantic_review_accepts(invalid):
    async def run():
        model = ReviewModel(
            translated(invalid),
            reviewed(),
            translated("Buksan ang pahina."),
            reviewed(),
        )
        records = []
        result = await LanguageConversionAgent(model).convert(
            "Open the page.",
            "en",
            "tl",
            mode="output",
            review_records=records,
        )
        assert result == "Buksan ang pahina."
        assert records[0]["failure"] == "invalid_public_translation"
        assert model.calls[-1][1]["repair_feedback"] is None

    asyncio.run(run())


def test_native_rewrite_cannot_bypass_public_validation():
    async def run():
        source = "Open the page."
        bad = "Maling sagot."
        model = ReviewModel(
            {"detected_language": "en", "target_language": "tl"},
            translated(bad),
            reviewed(source, bad, "different"),
            translated(bad),
            reviewed(source, bad, "different"),
        )
        bridge = LanguageBridge(model)
        turn = await bridge.prepare(source)

        async def rewrite(*args):
            return 'password: "PRIVATE_SECRET"'

        result = await bridge.render(turn, source, rewrite=rewrite)
        assert result == language_message("tl", "failure")
        assert turn.output_fallback["failureReason"] == "invalid_public_translation"
        assert "PRIVATE_SECRET" not in result

    asyncio.run(run())


def test_genuine_source_ambiguity_requests_clarification_without_guessing_or_retrying():
    async def run():
        source, candidate = "Bukas.", "Open."
        model = ReviewModel(
            translated(candidate), reviewed(source, candidate, "ambiguous")
        )
        with pytest.raises(LanguageBridgeError, match="translation_ambiguous"):
            await LanguageConversionAgent(model).convert(
                source, "tl", "en", mode="input"
            )
        assert len(model.calls) == 2

    asyncio.run(run())


@pytest.mark.parametrize(
    "bad_review",
    [
        {
            "verdict": "equivalent",
            "differences": [
                {
                    "source_quote": "Open",
                    "candidate_quote": "Buksan",
                    "explanation": "Contradictory",
                }
            ],
        },
        {"verdict": "different", "differences": []},
        {
            "verdict": "different",
            "differences": [
                {
                    "source_quote": "",
                    "candidate_quote": "",
                    "explanation": "No evidence",
                }
            ],
        },
        {"verdict": "equivalent", "differences": [], "execute": True},
        {"verdict": True, "differences": []},
    ],
)
def test_malformed_reviews_never_validate_a_translation(bad_review):
    async def run():
        model = ReviewModel(translated("Buksan ang pahina."), bad_review, bad_review)
        with pytest.raises(ValueError):
            await LanguageConversionAgent(model).convert(
                "Open the page.", "en", "tl", mode="output"
            )
        assert len(model.calls) == 3

    asyncio.run(run())


def test_review_must_quote_the_actual_source_and_candidate():
    async def run():
        model = ReviewModel(
            translated("Buksan ang pahina."),
            reviewed("Invented source", "pahina", "different"),
        )
        with pytest.raises(LanguageBridgeError, match="ungrounded_language_review"):
            await LanguageConversionAgent(model).convert(
                "Open the page.", "en", "tl", mode="output"
            )
        assert len(model.calls) == 2

    asyncio.run(run())


def test_malformed_review_gets_one_recheck_without_retranslating_a_valid_draft():
    async def run():
        model = ReviewModel(
            translated("Buksan ang pahina."),
            {"verdict": "equivalent", "differences": []},
            reviewed(),
        )
        records = []
        result = await LanguageConversionAgent(model).convert(
            "Open the page.", "en", "tl", mode="output", review_records=records
        )
        assert result == "Buksan ang pahina."
        assert [payload["mode"] for _, payload in model.calls] == [
            "output",
            "review",
            "review",
        ]
        assert records[0]["failure"] == "invalid_language_review"
        assert records[1]["review"]["verdict"] == "equivalent"

    asyncio.run(run())


def test_output_repair_is_reviewed_before_publication_and_cached_for_this_turn():
    async def run():
        source = "Review and confirm the action yourself."
        bad = "Ignore at kumpirmahin ang aksyon."
        correct = "Suriin at kumpirmahin mo mismo ang aksyon."
        model = ReviewModel(
            {"detected_language": "en", "target_language": "tl"},
            translated(bad),
            reviewed("Review", "Ignore", "different"),
            translated(correct),
            reviewed(),
        )
        bridge = LanguageBridge(model)
        turn = await bridge.prepare("Reply in Tagalog.")
        assert await bridge.render(turn, source) == correct
        assert await bridge.render(turn, source) == correct
        assert len(turn.output_reviews) == 2
        assert len(model.calls) == 5

    asyncio.run(run())


def test_persistent_output_mismatch_never_publishes_english_or_unreviewed_translation():
    async def run():
        source, bad = "Review the record.", "Burahin ang record."
        model = ReviewModel(
            {"detected_language": "en", "target_language": "tl"},
            translated(bad),
            reviewed(source, bad, "different"),
            translated(bad),
            reviewed(source, bad, "different"),
        )
        bridge = LanguageBridge(model)
        turn = await bridge.prepare("Reply in Tagalog.")
        assert await bridge.render(turn, source) == language_message("tl", "failure")
        assert turn.failure_reason == "semantic_mismatch"

    asyncio.run(run())


def test_identifier_word_order_can_change_without_masking():
    async def run():
        source = "Open APP-2001 for Company Two."
        candidate = "Para sa Company Two, buksan ang APP-2001."
        model = ReviewModel(translated(candidate), reviewed())
        assert (
            await LanguageConversionAgent(model).convert(
                source, "en", "tl", mode="output"
            )
            == candidate
        )
        assert model.calls[0][1]["text"] == source
        assert model.calls[1][1]["source"] == source

    asyncio.run(run())


def test_translation_and_review_receive_the_original_names_and_addresses():
    async def run():
        source = "Select Private Company at private@example.test."
        candidate = "Piliin ang Private Company sa private@example.test."
        model = ReviewModel(translated(candidate), reviewed())
        result = await LanguageConversionAgent(model).convert(
            source, "en", "tl", mode="output"
        )
        assert result == candidate
        assert model.calls[0][1]["text"] == source
        assert model.calls[1][1]["source"] == source
        assert model.calls[1][1]["candidate"] == candidate
        assert all(
            "tools" not in payload and "protected_spans" not in payload
            for _, payload in model.calls
        )

    asyncio.run(run())


def test_detection_rechecks_language_uncertainty_without_old_business_context():
    async def run():
        model = ReviewModel(
            {
                "detected_language": "en",
                "target_language": "en",
                "needs_clarification": True,
            },
            {
                "detected_language": "en",
                "target_language": "en",
                "needs_clarification": False,
            },
        )
        result = await LanguageDetector(model).identify(
            "Click the confirmation button for me.",
            "en",
            [{"role": "assistant", "content": "A confusing prior answer."}],
        )
        assert not result.needs_clarification
        assert model.calls[1][1]["context"] == []

    asyncio.run(run())


def test_cancellation_during_independent_review_is_not_converted_into_a_failure_reply():
    async def run():
        model = ReviewModel(translated("Buksan ang pahina."), asyncio.CancelledError())
        with pytest.raises(asyncio.CancelledError):
            await LanguageConversionAgent(model).convert(
                "Open the page.", "en", "tl", mode="output"
            )

    asyncio.run(run())


def test_pre_review_history_does_not_override_original_user_meaning():
    event = SimpleNamespace(
        seq=1,
        event_type="user.message",
        event_json={"content": "Original application enquiry"},
        created_at=None,
    )
    old = SimpleNamespace(
        record_type="language.input",
        request_id="r",
        payload={
            "sourceLanguage": "ml",
            "promptVersion": "language-bridge/2",
            "userEventSeq": 1,
            "workingText": "Review the appeal",
        },
    )
    assert (
        working_history([event], [old])[0].event_json["content"]
        == event.event_json["content"]
    )
    current = SimpleNamespace(
        record_type="language.input",
        request_id="r",
        payload={
            **old.payload,
            "promptVersion": PROMPT_VERSION,
            "workingText": "Check the application",
        },
    )
    assert (
        working_history([event], [current])[0].event_json["content"]
        == "Check the application"
    )


def test_equivalent_negative_paraphrase_in_output_reaches_semantic_review():
    async def run():
        source = "Open your records. Access is limited to this profile."
        candidate = "اپنے ریکارڈ کھولیں۔ دوسرے پروفائل کا ڈیٹا دستیاب نہیں ہے۔"
        model = ReviewModel(translated(candidate), reviewed())
        result = await LanguageConversionAgent(model).convert(
            source, "en", "ur", mode="output"
        )
        assert result == candidate
        assert model.calls[-1][1]["mode"] == "review"

    asyncio.run(run())


def test_output_polarity_reversal_is_still_rejected_by_independent_review():
    async def run():
        source, bad = (
            "Do not submit it; you may cancel it.",
            "Huwag kanselahin; isumite ito.",
        )
        model = ReviewModel(
            translated(bad),
            reviewed(source, bad, "different"),
            translated(bad),
            reviewed(source, bad, "different"),
        )
        with pytest.raises(LanguageBridgeError, match="semantic_mismatch"):
            await LanguageConversionAgent(model).convert(
                source, "en", "tl", mode="output"
            )

    asyncio.run(run())


def test_review_timeout_retries_only_the_review_and_keeps_audit():
    import httpx

    async def run():
        model = ReviewModel(
            translated("Buksan ang pahina."), httpx.ReadTimeout("private"), reviewed()
        )
        records = []
        result = await LanguageConversionAgent(model).convert(
            "Open the page.", "en", "tl", mode="output", review_records=records
        )
        assert result == "Buksan ang pahina."
        assert [p["mode"] for _, p in model.calls] == ["output", "review", "review"]
        assert records[0]["failure"] == "language_service_timeout"
        assert "private" not in str(records)

    asyncio.run(run())


def test_review_recheck_receives_schema_feedback():
    async def run():
        model = ReviewModel(
            translated("Buksan ang pahina."), {"verdict": "equivalent"}, reviewed()
        )
        await LanguageConversionAgent(model).convert(
            "Open the page.", "en", "tl", mode="output"
        )
        assert (
            model.calls[-1][1]["repair_feedback"]["failure"]
            == "invalid_language_review"
        )

    asyncio.run(run())


def test_translation_timeout_has_one_bounded_retry_and_permanent_failure_is_visible():
    import httpx

    async def run():
        model = ReviewModel(httpx.ReadTimeout("private"), httpx.ReadTimeout("private"))
        records = []
        with pytest.raises(httpx.ReadTimeout):
            await LanguageConversionAgent(model).convert(
                "Open the page.", "en", "tl", mode="output", review_records=records
            )
        assert len(model.calls) == 2
        assert [r["failure"] for r in records] == ["language_service_timeout"] * 2

    asyncio.run(run())


@pytest.mark.parametrize(
    "source,bad,correct",
    [
        ("Do not submit it.", "Isumite ito.", "Huwag isumite ito."),
        ("Open your requests.", "Open your requests.", "Buksan ang iyong mga request."),
        ("Open APP-2001.", "Buksan ang APP-2002.", "Buksan ang APP-2001."),
        ("Amount: 25.00.", "Halaga: 30.00.", "Halaga: 25.00."),
        (
            "Open [Requests](/requests).",
            "Buksan ang Requests [/requests].",
            "Buksan ang [Requests](/requests).",
        ),
        (
            "Email user@example.test.",
            "Mag-email sa other@example.test.",
            "Mag-email sa user@example.test.",
        ),
    ],
)
def test_independent_reviewer_handles_fidelity_without_regex_rules(
    source, bad, correct
):
    async def run():
        model = ReviewModel(
            translated(bad),
            reviewed(source, bad, "different"),
            translated(correct),
            reviewed(),
        )
        records = []
        answer = await LanguageConversionAgent(model).convert(
            source, "en", "tl", mode="output", review_records=records
        )
        assert answer == correct
        assert [payload["mode"] for _, payload in model.calls] == [
            "output",
            "review",
            "output",
            "review",
        ]
        assert records[0]["failure"] == "semantic_mismatch"
        assert model.calls[1][1]["source"] == source
        assert model.calls[1][1]["candidate"] == bad

    asyncio.run(run())


def test_failed_output_can_use_one_reviewed_native_answer_without_replaying_work():
    async def run():
        source, bad, good = (
            "Review the record.",
            "Burahin ang record.",
            "Suriin ang record.",
        )
        model = ReviewModel(
            {"detected_language": "en", "target_language": "tl"},
            translated(bad),
            reviewed(source, bad, "different"),
            translated(bad),
            reviewed(source, bad, "different"),
            reviewed(),
        )
        bridge = LanguageBridge(model)
        turn = await bridge.prepare("Reply in Tagalog.")
        calls = []

        async def rewrite(original, answer, language):
            calls.append((original, answer, language))
            return good

        assert await bridge.render(turn, source, rewrite=rewrite) == good
        assert await bridge.render(turn, source, rewrite=rewrite) == good
        assert calls == [("Reply in Tagalog.", source, "tl")]
        assert turn.failure is None
        assert turn.output_fallback["trigger"] == "semantic_mismatch"
        assert turn.output_fallback["status"] == "accepted"

    asyncio.run(run())


@pytest.mark.parametrize("candidate", ["", "Delete it."])
def test_native_answer_failure_never_publishes_an_unreviewed_draft(candidate):
    async def run():
        model = ReviewModel(
            {"detected_language": "en", "target_language": "tl"},
            translated("Wrong."),
            reviewed("Review it.", "Wrong.", "different"),
            translated("Wrong."),
            reviewed("Review it.", "Wrong.", "different"),
            reviewed("Review it.", candidate, "different"),
        )
        bridge = LanguageBridge(model)
        turn = await bridge.prepare("Reply in Tagalog.")
        calls = []

        async def rewrite(*args):
            calls.append(args)
            return candidate

        assert await bridge.render(
            turn, "Review it.", rewrite=rewrite
        ) == language_message("tl", "failure")
        assert len(calls) == 1
        assert turn.output_fallback["status"] == "failed"
        assert await bridge.render(
            turn, "Review it.", rewrite=rewrite
        ) == language_message("tl", "failure")
        assert len(calls) == 1

    asyncio.run(run())


def test_provider_timeout_stops_without_another_main_model_request():
    import httpx

    async def run():
        model = ReviewModel(
            {"detected_language": "en", "target_language": "tl"},
            httpx.ReadTimeout("private"),
            httpx.ReadTimeout("private"),
        )
        bridge = LanguageBridge(model)
        turn = await bridge.prepare("Reply in Tagalog.")

        async def rewrite(*args):
            raise AssertionError("must not call an unavailable model again")

        assert await bridge.render(
            turn, "Review it.", rewrite=rewrite
        ) == language_message("tl", "failure")
        assert turn.failure_reason == "language_service_timeout"
        assert turn.output_fallback["status"] == "skipped"

    asyncio.run(run())


def test_input_failure_never_calls_native_answer_recovery():
    async def run():
        model = ReviewModel(RuntimeError("private"))
        bridge = LanguageBridge(model)
        turn = await bridge.prepare("Ipakita ang aking mga aplikasyon.")

        async def rewrite(*args):
            raise AssertionError("input failure must not enter business generation")

        assert await bridge.render(turn, "", rewrite=rewrite) == language_message(
            "en", "failure"
        )
        assert turn.output_fallback == {}

    asyncio.run(run())


def test_native_answer_cancellation_propagates():
    async def run():
        model = ReviewModel(
            {"detected_language": "en", "target_language": "tl"},
            ValueError("invalid result"),
        )
        bridge = LanguageBridge(model)
        turn = await bridge.prepare("Reply in Tagalog.")

        async def rewrite(*args):
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await bridge.render(turn, "Review it.", rewrite=rewrite)

    asyncio.run(run())

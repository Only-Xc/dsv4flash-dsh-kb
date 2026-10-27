"""Real service and SQLite persistence with isolated model/tool boundaries."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.config import Settings
from app.db import AuditRecord, Base
from app.language_history import load_language_records, working_history
from app.language_messages import language_message
from app.principal import Principal
from app.runtime import RuntimeManager
from app.service import DSHService, EventBroker
from app.service_fees import ServiceFeeEntry, ServiceFeeResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


class LanguageModel:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.answer = (
            "You can sign in using UAE PASS or your registered email and password."
        )
        self.messages = []

    async def language_task(self, system, payload):
        self.calls.append(payload)
        if payload.get("mode") == "review":
            return {
                "source_meaning": "The supplied source proposition.",
                "candidate_meaning": "The supplied source proposition.",
                "verdict": "equivalent",
                "differences": [],
            }
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def stream(self, messages, **kwargs):
        self.messages = messages
        yield self.answer


def translated(text):
    return {"text": text}


class LanguageTurnTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_incomplete_answer_is_not_translated_or_marked_complete(self):
        class InterruptedModel(LanguageModel):
            async def stream(model, messages, **kwargs):
                yield "There are two login methods.\n\n"
                raise TimeoutError("private provider information")

        self.service.llm = InterruptedModel(
            {"detected_language": "tl", "target_language": "tl"},
            translated("How do I log in?"),
        )
        await self.add_question("Paano ako mag-log in?")
        await self.run_turn()
        events = await self.events()
        answer = next(e.event_json for e in events if e.event_type == "assistant.message")
        self.assertEqual(answer["streamStatus"], "failed")
        self.assertEqual(answer["content"], language_message("tl", "failure"))
        self.assertFalse(any(c.get("mode") == "output" for c in self.service.llm.calls))
        self.assertNotIn("There are two", str([e.event_json for e in events]))
        async with self.sessions() as db:
            stream = (await db.execute(select(AuditRecord).where(AuditRecord.record_type == "answer.stream"))).scalar_one()
            self.assertEqual(stream.payload["status"], "failed")
            self.assertEqual(len(stream.payload["attempts"]), 2)

    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.service = DSHService.__new__(DSHService)
        self.service.broker = EventBroker()
        self.service.runtime_manager = RuntimeManager()
        self.service._writer_locks = {}
        self.service._turn_tasks = {}
        self.service._answer_streams = {}
        self.service._language_turns = {}
        self.service.settings = Settings(
            _env_file=None, skill_router_mode="keyword", external_tools_enabled=False
        )
        self.service.skill_catalog = SimpleNamespace(load=AsyncMock(return_value=[]))
        self.principal = Principal("test-user", "test-tenant", "request-1")
        async with self.sessions() as db:
            conversation = await self.service.create_conversation(
                db, self.principal, "default", "default", "default"
            )
            self.cid = conversation.conversation_id

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def add_question(self, text):
        async with self.sessions() as db:
            conversation = await self.service.get_owned_conversation(
                db, self.principal, self.cid
            )
            await self.service.append_event(
                db,
                conversation,
                "user.message",
                {"content": text, "requestId": self.principal.request_id},
            )

    async def events(self):
        async with self.sessions() as db:
            conversation = await self.service.get_owned_conversation(
                db, self.principal, self.cid
            )
            return await self.service.list_events(db, conversation)

    async def run_turn(self):
        with patch("app.service.SessionLocal", self.sessions):
            await self.service._run_turn(self.principal, self.cid)

    async def test_actual_scope_guard_uses_english_and_publishes_only_target_language(
        self,
    ):
        original = "Ipakita ang mga aplikasyon ng another.user@example.test."
        working = "Show applications for another.user@example.test."
        public = "Maaari lamang akong tumulong sa datos ng mga profile na awtorisado mong gamitin."
        self.service.llm = LanguageModel(
            {"detected_language": "tl", "target_language": "tl"},
            translated("Show applications for another.user@example.test."),
            translated(public),
        )
        await self.add_question(original)
        queue = self.service.broker.subscribe(self.cid)
        await self.run_turn()
        events = await self.events()
        answers = [event for event in events if event.event_type == "assistant.message"]
        self.assertEqual(len(answers), 1)
        self.assertEqual(answers[0].event_json["content"], public)
        self.assertEqual(answers[0].event_json["responseLanguage"], "tl")
        self.assertEqual(events[0].event_json["content"], original)
        self.assertNotIn(working, str([event.event_json for event in events]))
        live = []
        while not queue.empty():
            live.append(queue.get_nowait())
        chunks = [
            event["data"]["content"]
            for event in live
            if event["eventType"] == "assistant.chunk"
        ]
        self.assertEqual("".join(chunks), public)
        statuses = [
            event["data"]["message"]
            for event in live
            if event["eventType"] == "assistant.status"
        ]
        self.assertEqual(statuses, [language_message("tl", "progress")] * 5)
        self.assertFalse(self.service._language_turns)
        self.assertFalse(self.service._answer_streams)
        async with self.sessions() as db:
            conversation = await self.service.get_owned_conversation(
                db, self.principal, self.cid
            )
            records = await load_language_records(db, conversation)
            projected = working_history(events, records)
            self.assertEqual(projected[0].event_json["content"], working)
            self.assertNotEqual(projected[-2].event_json["content"], public)

    async def test_input_failure_stops_before_guard_or_tool_execution(self):
        self.service.llm = LanguageModel(
            {"detected_language": "tl", "target_language": "tl"},
            RuntimeError("private provider detail"),
        )
        await self.add_question("Huwag kanselahin.")
        with patch.object(
            self.service,
            "profile_scope_guard",
            side_effect=AssertionError("must not route"),
        ):
            await self.run_turn()
        events = await self.events()
        answers = [
            event.event_json["content"]
            for event in events
            if event.event_type == "assistant.message"
        ]
        self.assertEqual(answers, [language_message("tl", "failure")])
        answer = next(e.event_json for e in events if e.event_type == "assistant.message")
        self.assertEqual(answer["streamStatus"], "failed")
        self.assertEqual(answer["failureReason"], "language_service_unavailable")
        self.assertFalse(
            any(
                event.event_type in {"tool.call", "skill.route", "runtime.error"}
                for event in events
            )
        )
        self.assertNotIn(
            "private provider detail", str([event.event_json for event in events])
        )

    async def test_generated_english_is_buffered_until_output_conversion_finishes(self):
        original = "Paano ako mag-log in?"
        working = "How can I log in?"
        public = "Maaari kang mag-sign in gamit ang UAE PASS o ang iyong nakarehistrong email at password."
        self.service.llm = LanguageModel(
            {"detected_language": "tl", "target_language": "tl"},
            translated(working),
            translated(public),
        )
        await self.add_question(original)
        queue = self.service.broker.subscribe(self.cid)
        await self.run_turn()
        events = await self.events()
        answers = [
            event.event_json["content"]
            for event in events
            if event.event_type == "assistant.message"
        ]
        self.assertEqual(answers, [public])
        live = []
        while not queue.empty():
            live.append(queue.get_nowait())
        self.assertNotIn(self.service.llm.answer, str(live))
        self.assertIn(working, str(self.service.llm.messages))
        self.assertIn(original, str(self.service.llm.messages))
        async with self.sessions() as db:
            audit = (
                await db.execute(
                    select(AuditRecord).where(
                        AuditRecord.record_type == "language.output"
                    )
                )
            ).scalar_one()
            self.assertEqual(audit.payload["workingText"], self.service.llm.answer)
            self.assertEqual(
                audit.payload["reviews"][-1]["review"]["verdict"], "equivalent"
            )

    async def test_semantic_review_failure_cannot_reach_business_routing(self):
        class RejectingReviewModel(LanguageModel):
            async def language_task(model, system, payload):
                if payload.get("mode") == "review":
                    return {
                        "source_meaning": "Show existing applications.",
                        "candidate_meaning": "Delete existing applications.",
                        "verdict": "different",
                        "differences": [
                            {
                                "source_quote": payload["source"],
                                "candidate_quote": payload["candidate"],
                                "explanation": "A request to show records became a deletion request.",
                            }
                        ],
                    }
                return await super().language_task(system, payload)

        self.service.llm = RejectingReviewModel(
            {"detected_language": "tl", "target_language": "tl"},
            translated("Delete my applications."),
            translated("Delete my applications."),
        )
        await self.add_question("Ipakita ang aking mga aplikasyon.")
        with patch.object(
            self.service,
            "profile_scope_guard",
            side_effect=AssertionError("must not route"),
        ):
            await self.run_turn()
        events = await self.events()
        self.assertFalse(
            any(event.event_type in {"tool.call", "skill.route"} for event in events)
        )
        answers = [
            event.event_json["content"]
            for event in events
            if event.event_type == "assistant.message"
        ]
        self.assertEqual(answers, [language_message("tl", "failure")])

    async def test_generated_urdu_and_malayalam_preserve_public_language(self):
        samples = [
            (
                "ur",
                "میں لاگ ان کیسے کروں؟",
                "آپ UAE PASS کے ذریعے لاگ ان کر سکتے ہیں۔",
                "rtl",
            ),
            (
                "ml",
                "എങ്ങനെ ലോഗിൻ ചെയ്യാം?",
                "നിങ്ങൾക്ക് UAE PASS ഉപയോഗിച്ച് ലോഗിൻ ചെയ്യാം.",
                "ltr",
            ),
        ]
        for language, original, public, direction in samples:
            with self.subTest(language=language):
                async with self.sessions() as db:
                    conversation = await self.service.create_conversation(
                        db, self.principal, "default", "default", "default"
                    )
                    self.cid = conversation.conversation_id
                self.service.llm = LanguageModel(
                    {"detected_language": language, "target_language": language},
                    translated("How can I log in?"),
                    translated(public),
                )
                self.service.llm.answer = "You can sign in using UAE PASS."
                await self.add_question(original)
                await self.run_turn()
                answers = [
                    event.event_json
                    for event in await self.events()
                    if event.event_type == "assistant.message"
                ]
                self.assertEqual(answers[0]["content"], public)
                self.assertEqual(answers[0]["direction"], direction)
                self.assertEqual(answers[0]["responseLanguage"], language)

    async def test_output_failure_replaces_the_private_draft_with_a_safe_receipt(self):
        self.service.llm = LanguageModel(
            {"detected_language": "en", "target_language": "tl"},
            RuntimeError("provider private error"),
        )
        await self.add_question("How can I log in? Reply in Tagalog.")
        await self.run_turn()
        events = await self.events()
        answer = next(
            event.event_json
            for event in events
            if event.event_type == "assistant.message"
        )
        self.assertEqual(answer["content"], language_message("tl", "failure"))
        self.assertEqual(answer["streamStatus"], "failed")
        self.assertNotIn(
            self.service.llm.answer, str([event.event_json for event in events])
        )

    async def test_fee_reply_uses_the_original_business_identity_and_output_boundary(
        self,
    ):
        self.service.settings.external_tools_enabled = True
        self.principal = Principal(
            "test-user", "test-tenant", "request-1", umc_token="test-only-token"
        )
        self.service.llm = LanguageModel(
            {"detected_language": "tl", "target_language": "tl"},
            translated("What is the fee for service ID 35?"),
            translated(
                "## Permit\n\nBayad sa serbisyo: **AED 25.00**\n\n"
                "Pinagmulan: [Service card — Learn More](/services/35)\n\n"
                "Ito ang pampublikong bayad, hindi balanse o invoice. Walang aplikasyon o pagbabayad na ginawa."
            ),
        )
        result = ServiceFeeResponse(
            status="matched",
            services=[
                ServiceFeeEntry(
                    serviceId=35,
                    nameEn="Permit",
                    nameAr="Permit",
                    feeEn="AED 25.00",
                    sourcePath="/services/35",
                )
            ],
        )
        await self.add_question("Magkano ang bayad sa service ID 35?")
        with patch(
            "app.service.CustomerServiceFeeClient.lookup",
            new_callable=AsyncMock,
            return_value=result,
        ) as lookup:
            await self.run_turn()
        lookup.assert_awaited_once_with(
            "What is the fee for service ID 35?", umc_token="test-only-token"
        )
        events = await self.events()
        answer = next(
            event.event_json["content"]
            for event in events
            if event.event_type == "assistant.message"
        )
        self.assertIn("Bayad sa serbisyo: **AED 25.00**", answer)
        self.assertIn("(/services/35)", answer)
        self.assertNotIn("test-only-token", str(self.service.llm.calls))
        self.assertEqual(self.service.llm.messages, [])

    async def test_cancelling_output_conversion_never_publishes_the_private_draft(self):
        started = asyncio.Event()

        class BlockingOutputModel(LanguageModel):
            async def language_task(model, system, payload):
                if payload.get("mode") == "output":
                    started.set()
                    await asyncio.Event().wait()
                return await super().language_task(system, payload)

        self.service.llm = BlockingOutputModel(
            {"detected_language": "en", "target_language": "ur"}
        )
        await self.add_question("How can I log in? Reply in Urdu.")
        with patch("app.service.SessionLocal", self.sessions):
            task = asyncio.create_task(self.service._run_turn(self.principal, self.cid))
            self.service._turn_tasks[self.cid] = task
            await asyncio.wait_for(started.wait(), timeout=2)
            await self.service.cancel(self.principal, self.cid)
            with self.assertRaises(asyncio.CancelledError):
                await task
        events = await self.events()
        answers = [
            event.event_json["content"]
            for event in events
            if event.event_type == "assistant.message"
        ]
        self.assertEqual(answers, [language_message("ur", "cancelled")])
        self.assertFalse(self.service._language_turns)
        self.assertFalse(self.service._answer_streams)

    async def test_language_audit_query_is_scoped_to_owner(self):
        async with self.sessions() as db:
            conversation = await self.service.get_owned_conversation(
                db, self.principal, self.cid
            )
            db.add(
                AuditRecord(
                    tenant_id="other-tenant",
                    user_id="other-user",
                    conversation_id=self.cid,
                    dsh_session_id="other-session",
                    category="runtime",
                    record_type="language.input",
                    payload={
                        "workingText": "Foreign private question",
                        "userEventSeq": 1,
                    },
                )
            )
            await db.commit()
            self.assertEqual(await load_language_records(db, conversation), [])

    async def test_history_projection_preserves_tool_evidence_and_source_objects(self):
        user = SimpleNamespace(
            seq=1,
            event_type="user.message",
            event_json={"content": "Kumusta"},
            created_at=None,
        )
        tool = SimpleNamespace(
            seq=2,
            event_type="tool.result",
            event_json={"result": {"id": "APP-1"}},
            created_at=None,
        )
        audit = SimpleNamespace(
            record_type="language.input",
            request_id="r",
            payload={"userEventSeq": 1, "workingText": "Hello"},
        )
        projected = working_history([user, tool], [audit])
        self.assertEqual(projected[0].event_json["content"], "Hello")
        self.assertEqual(user.event_json["content"], "Kumusta")
        self.assertEqual(projected[1].event_json, tool.event_json)

    async def test_attachment_only_turn_keeps_the_native_entry_path(self):
        self.service.llm = LanguageModel()
        async with self.sessions() as db:
            conversation = await self.service.get_owned_conversation(
                db, self.principal, self.cid
            )
            await self.service.append_event(
                db,
                conversation,
                "user.message",
                {
                    "content": "",
                    "attachment": {"fileRef": "private-document", "fileType": 0},
                },
            )
            history = await self.service.list_events(db, conversation)
            projected, turn = await self.service.prepare_language_turn(
                db, conversation, history, "request-1"
            )
            self.assertFalse(turn.failure)
            self.assertEqual(turn.working_language, "en")
            self.assertEqual(
                projected[-1].event_json["attachment"]["fileRef"], "private-document"
            )
            self.assertEqual(self.service.llm.calls, [])

    async def test_language_and_private_working_context_survive_service_recreation(
        self,
    ):
        self.service.llm = LanguageModel(
            {"detected_language": "tl", "target_language": "tl"},
            translated("How can I log in?"),
            translated("Mag-sign in gamit ang UAE PASS."),
        )
        self.service.llm.answer = "Sign in using UAE PASS."
        await self.add_question("Paano ako mag-log in?")
        await self.run_turn()
        self.principal = Principal("test-user", "test-tenant", "request-2")
        await self.add_question("2")
        restarted = DSHService.__new__(DSHService)
        restarted.llm = LanguageModel(
            {"detected_language": "tl", "target_language": "tl"}, translated("2")
        )
        async with self.sessions() as db:
            conversation = await self.service.get_owned_conversation(
                db, self.principal, self.cid
            )
            history = await self.service.list_events(db, conversation)
            projected, turn = await restarted.prepare_language_turn(
                db, conversation, history, "request-2"
            )
        self.assertEqual(turn.target_language, "tl")
        self.assertEqual(turn.working_text, "2")
        self.assertEqual(projected[0].event_json["content"], "How can I log in?")
        self.assertIn(
            "Sign in using UAE PASS.",
            [event.event_json.get("content") for event in projected],
        )
        self.assertEqual(restarted.llm.calls[0]["previous_language"], "tl")

    async def test_pre_upgrade_arabic_history_keeps_short_followups_native(self):
        self.service.llm = LanguageModel(
            {"detected_language": "ar", "target_language": "ar"}
        )
        async with self.sessions() as db:
            conversation = await self.service.get_owned_conversation(
                db, self.principal, self.cid
            )
            await self.service.append_event(
                db,
                conversation,
                "assistant.message",
                {"content": "اختر الطلب المطلوب من القائمة."},
            )
        await self.add_question("2")
        async with self.sessions() as db:
            conversation = await self.service.get_owned_conversation(
                db, self.principal, self.cid
            )
            history = await self.service.list_events(db, conversation)
            _, turn = await self.service.prepare_language_turn(
                db, conversation, history, "request-1"
            )
        self.assertEqual(turn.working_language, "ar")
        self.assertFalse(turn.requires_bridge)
        self.assertEqual(self.service.llm.calls[0]["previous_language"], "ar")

    async def test_native_generation_uses_no_input_or_output_conversion(self):
        for language, question, answer in [
            ("en", "How can I log in?", "You can sign in using UAE PASS."),
            ("ar", "كيف يمكنني تسجيل الدخول؟", "يمكنك تسجيل الدخول باستخدام UAE PASS."),
        ]:
            with self.subTest(language=language):
                async with self.sessions() as db:
                    conversation = await self.service.create_conversation(
                        db, self.principal, "default", "default", "default"
                    )
                    self.cid = conversation.conversation_id
                self.service.llm = LanguageModel(
                    {"detected_language": language, "target_language": language}
                )
                self.service.llm.answer = answer
                await self.add_question(question)
                await self.run_turn()
                messages = [
                    event.event_json
                    for event in await self.events()
                    if event.event_type == "assistant.message"
                ]
                self.assertEqual(messages[0]["content"], answer)
                self.assertEqual(len(self.service.llm.calls), 1)

    async def test_output_recovery_uses_main_model_without_replaying_business_tools(
        self,
    ):
        import json

        public = "Maaari kang mag-sign in gamit ang UAE PASS o email at password."

        class RecoveringModel(LanguageModel):
            def __init__(model):
                super().__init__({"detected_language": "en", "target_language": "tl"})
                model.generations = []

            async def language_task(model, system, payload):
                if payload.get("mode") == "output":
                    model.calls.append(payload)
                    raise ValueError("private malformed translation")
                return await super().language_task(system, payload)

            async def stream(model, messages, **kwargs):
                model.generations.append(messages)
                yield model.answer if len(model.generations) == 1 else public

        self.service.llm = RecoveringModel()
        await self.add_question("How can I log in? Reply in Tagalog.")
        queue = self.service.broker.subscribe(self.cid)
        await self.run_turn()
        events = await self.events()
        answer = next(
            event.event_json
            for event in events
            if event.event_type == "assistant.message"
        )
        self.assertEqual(answer["content"], public)
        self.assertEqual(answer["streamStatus"], "complete")
        self.assertFalse(any(event.event_type == "tool.call" for event in events))
        self.assertEqual(len(self.service.llm.generations), 2)
        recovery = json.loads(self.service.llm.generations[-1][1]["content"])
        self.assertEqual(recovery["verified_answer"], self.service.llm.answer)
        self.assertEqual(recovery["target_language"], "Tagalog")
        self.assertNotIn("token", recovery)
        chunks = []
        while not queue.empty():
            event = queue.get_nowait()
            if event["eventType"] == "assistant.chunk":
                chunks.append(event["data"]["content"])
        self.assertEqual("".join(chunks), public)
        async with self.sessions() as db:
            audit = (
                await db.execute(
                    select(AuditRecord).where(
                        AuditRecord.record_type == "language.output"
                    )
                )
            ).scalar_one()
            self.assertIsNone(audit.payload["failure"])
            self.assertEqual(audit.payload["fallback"]["status"], "accepted")
            self.assertEqual(
                audit.payload["fallback"]["trigger"], "invalid_language_result"
            )

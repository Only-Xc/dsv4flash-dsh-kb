"""Reproduce topic loss and execution promises at the existing routing seams."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from app.config import Settings
from app.llm import LLMAdapter
from app.profile_scope import ProfileContext
from app.service import DSHService
from app.skill_router import recall_skill_candidates
from app.skills import SkillRoute, build_system_prompt


@pytest.mark.parametrize(
    "question,is_read",
    [
        ("Can the chatbot submit this request on my behalf?", False),
        ("Submit my application for me.", False),
        ("Show my requests.", True),
        ("Check my application status.", True),
        ("اعرض طلباتي", True),
        ("my applications", True),
        ("طلباتي", True),
    ],
)
def test_only_a_record_read_can_force_the_application_list_route(question, is_read):
    assert DSHService.is_application_list_request(question) is is_read


def test_global_view_does_not_mistake_a_procedural_question_for_a_record_lookup():
    context = ProfileContext("0", "Global View", True, ())
    assert not DSHService.is_global_profile_record_request(
        "Can the chatbot submit this request on my behalf?", context
    )
    assert DSHService.is_global_profile_record_request("Show my requests.", context)
    assert DSHService.is_global_profile_record_request("my licenses", context)


def catalog():
    return [
        {
            "skillId": "payment_receipt",
            "domain": "payments",
            "aliases": [],
            "allowedTools": ["umc.payments"],
            "deterministicRouting": [
                {
                    "priority": 970,
                    "anyTerms": ["refund"],
                    "route": {
                        "category": "portal_action",
                        "mode": "portal_action",
                        "routingLocked": True,
                    },
                }
            ],
        },
        {
            "skillId": "service_discovery",
            "domain": "services",
            "aliases": ["find a service"],
            "allowedTools": ["knowledge.search"],
        },
        {
            "skillId": "general_knowledge",
            "domain": "general",
            "aliases": [],
            "allowedTools": ["knowledge.search"],
        },
    ]


def test_a_published_active_skill_remains_a_candidate_for_an_elliptical_followup():
    context = {
        "activeSkillId": "payment_receipt",
        "activeDomain": "payments",
        "recentMessages": [
            {"role": "user", "content": "How do I submit a refund request?"}
        ],
    }
    recall = recall_skill_candidates("Can you do it for me?", catalog(), context)
    assert "payment_receipt" in [item["skillId"] for item in recall.candidates]
    changed_topic = recall_skill_candidates("Find a service.", catalog(), context)
    assert "service_discovery" in [item["skillId"] for item in changed_topic.candidates]
    unknown = recall_skill_candidates(
        "Can you do it?", catalog(), {"activeSkillId": "unpublished"}
    )
    assert not unknown.candidates


def test_router_resolves_the_followup_and_reuses_published_portal_action_rules():
    async def run():
        service = object.__new__(DSHService)
        service.settings = SimpleNamespace(
            skill_router_mode="llm", skill_router_fallback_skill_id="general_knowledge"
        )
        service.skill_catalog = SimpleNamespace(load=AsyncMock(return_value=catalog()))
        service.llm = SimpleNamespace(
            route_skill=AsyncMock(
                return_value={
                    "skillId": "payment_receipt",
                    "confidence": 0.96,
                    "resolvedQuestion": "Can the chatbot submit the refund request on my behalf?",
                }
            )
        )
        route, metadata = await service.choose_skill_route(
            None,
            "Can it do this for me?",
            SkillRoute("general", "general"),
            None,
            "fixture-request",
            {"activeSkillId": "payment_receipt", "activeDomain": "payments"},
        )
        assert route.skill_id == "payment_receipt"
        assert route.mode == "portal_action"
        assert "refund" in metadata["resolvedQuestion"]
        candidates = service.llm.route_skill.call_args.args[1]
        assert "general_knowledge" in [item["skillId"] for item in candidates]

    asyncio.run(run())


def test_explicit_topic_change_does_not_fall_back_to_the_old_action_on_router_outage():
    async def run():
        service = object.__new__(DSHService)
        service.settings = SimpleNamespace(
            skill_router_mode="llm", skill_router_fallback_skill_id="general_knowledge"
        )
        service.skill_catalog = SimpleNamespace(load=AsyncMock(return_value=catalog()))
        service.llm = SimpleNamespace(route_skill=AsyncMock(side_effect=TimeoutError()))
        route, _ = await service.choose_skill_route(
            None,
            "Find a service.",
            SkillRoute("general", "general"),
            None,
            "fixture-request",
            {"activeSkillId": "payment_receipt", "activeDomain": "payments"},
        )
        assert route.skill_id != "payment_receipt"

    asyncio.run(run())


@pytest.mark.parametrize("mode,expected_count", [("answer", 1), ("portal_action", 0)])
def test_action_capabilities_come_from_the_runtime_not_confirmation_alone(
    mode, expected_count
):
    prompt = build_system_prompt(
        SkillRoute("controlled_draft", "api_call", mode=mode),
        evidence_available=True,
        available_tools=[
            {"name": "query", "sideEffect": "read", "description": "Read records."},
            {
                "name": "draft",
                "sideEffect": "write",
                "confirmationRequired": True,
                "description": "Create a test draft only, never submit formally.",
                "parameters": {"private": "not-for-answer-model"},
            },
        ],
    )
    actions = json.loads(
        next(
            line.removeprefix("RUNTIME_EXECUTABLE_ACTIONS: ")
            for line in prompt.splitlines()
            if line.startswith("RUNTIME_EXECUTABLE_ACTIONS: ")
        )
    )
    assert len(actions) == expected_count
    assert "not-for-answer-model" not in prompt
    if actions:
        assert (
            actions[0]["description"]
            == "Create a test draft only, never submit formally."
        )
        assert actions[0]["confirmationRequired"] is True


def test_router_json_call_preserves_resolved_question_and_uses_bounded_qwen_options():
    settings = Settings(
        _env_file=None,
        llm_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        llm_model="qwen3.8-flash",
        llm_api_key="test-only-key",
    )
    context = {
        "recentMessages": [{"role": "user", "content": "How do I find a service?"}]
    }

    def handler(request):
        payload = json.loads(request.content)
        assert payload["enable_thinking"] is False
        assert "tools" not in payload
        assert json.loads(payload["messages"][1]["content"])["context"] == context
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "skillId": "service_discovery",
                                    "confidence": 0.9,
                                    "resolvedQuestion": "Can I filter services by name and category?",
                                }
                            )
                        }
                    }
                ]
            },
        )

    real_client = httpx.AsyncClient
    with patch(
        "app.llm.httpx.AsyncClient",
        side_effect=lambda **kw: real_client(
            transport=httpx.MockTransport(handler), **kw
        ),
    ):
        result = asyncio.run(
            LLMAdapter(settings).route_skill("Can I filter them?", catalog(), context)
        )
    assert result["resolvedQuestion"] == "Can I filter services by name and category?"


@pytest.mark.parametrize("resolved", ["An invented procedure", {"query": "invalid"}])
def test_router_never_expands_a_first_question_and_rejects_nontext_rewrites(resolved):
    settings = Settings(
        _env_file=None,
        llm_base_url="https://provider.invalid/v1",
        llm_model="fixture",
        llm_api_key="fixture",
    )

    def handler(request):
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "skillId": "service_discovery",
                                    "confidence": 0.9,
                                    "resolvedQuestion": resolved,
                                }
                            )
                        }
                    }
                ]
            },
        )

    real_client = httpx.AsyncClient
    with patch(
        "app.llm.httpx.AsyncClient",
        side_effect=lambda **kw: real_client(
            transport=httpx.MockTransport(handler), **kw
        ),
    ):
        if isinstance(resolved, dict):
            with pytest.raises(ValueError, match="must be text"):
                asyncio.run(
                    LLMAdapter(settings).route_skill("Find Cedar Permit", catalog())
                )
        else:
            result = asyncio.run(
                LLMAdapter(settings).route_skill("Find Cedar Permit", catalog())
            )
            assert result["resolvedQuestion"] == "Find Cedar Permit"

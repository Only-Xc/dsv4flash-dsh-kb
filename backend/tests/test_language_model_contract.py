"""HTTP contract for language-only model calls, without external traffic."""

import asyncio
import json
from unittest.mock import patch

import httpx
import pytest
from app.config import Settings
from app.llm import LLMAdapter


def adapter():
    return LLMAdapter(
        Settings(
            _env_file=None,
            llm_base_url="https://provider.test/v1",
            llm_api_key="test-only-key",
            llm_model="configured-model",
        )
    )


def test_language_task_has_no_tools_or_business_identity():
    def handler(request):
        payload = json.loads(request.content)
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer test-only-key"
        assert payload["model"] == "configured-model"
        assert payload["stream"] is False
        assert payload["response_format"] == {"type": "json_object"}
        assert "tools" not in payload
        assert json.loads(payload["messages"][1]["content"]) == {"text": "Kumusta"}
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"detected_language":"tl"}'}}]},
        )

    client = httpx.AsyncClient
    with patch(
        "app.llm.httpx.AsyncClient",
        side_effect=lambda **kwargs: client(
            transport=httpx.MockTransport(handler), **kwargs
        ),
    ):
        result = asyncio.run(
            adapter().language_task("Identify language only.", {"text": "Kumusta"})
        )
    assert result == {"detected_language": "tl"}


@pytest.mark.parametrize(
    "body",
    [
        {"choices": []},
        {"choices": [{"message": {"content": "[]"}}]},
        {"choices": [{"message": {"content": "not JSON"}}]},
    ],
)
def test_invalid_provider_results_do_not_become_translations(body):
    client = httpx.AsyncClient
    with (
        patch(
            "app.llm.httpx.AsyncClient",
            side_effect=lambda **kwargs: client(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, json=body)
                ),
                **kwargs,
            ),
        ),
        pytest.raises(ValueError),
    ):
        asyncio.run(
            adapter().language_task("Identify language only.", {"text": "Kumusta"})
        )


def test_authentication_failure_is_not_treated_as_language_output():
    client = httpx.AsyncClient
    with (
        patch(
            "app.llm.httpx.AsyncClient",
            side_effect=lambda **kwargs: client(
                transport=httpx.MockTransport(lambda request: httpx.Response(401)),
                **kwargs,
            ),
        ),
        pytest.raises(httpx.HTTPStatusError),
    ):
        asyncio.run(
            adapter().language_task("Identify language only.", {"text": "Kumusta"})
        )


@pytest.mark.parametrize(
    "base_url,model,expected",
    [
        ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen3.8-flash", False),
        ("https://provider.test/v1", "configured-model", None),
        ("https://dashscope.aliyuncs.com/compatible-mode/v1", "other-model", None),
    ],
)
def test_only_dashscope_language_tasks_disable_qwen_thinking(base_url, model, expected):
    def handler(request):
        assert json.loads(request.content).get("enable_thinking") is expected
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    client = httpx.AsyncClient
    configured = LLMAdapter(
        Settings(
            _env_file=None,
            llm_base_url=base_url,
            llm_api_key="test-only-key",
            llm_model=model,
        )
    )
    with patch(
        "app.llm.httpx.AsyncClient",
        side_effect=lambda **kwargs: client(
            transport=httpx.MockTransport(handler), **kwargs
        ),
    ):
        asyncio.run(
            configured.language_task("Identify language only.", {"text": "Kumusta"})
        )


def test_language_task_honors_configured_timeout_instead_of_fifteen_second_cap():
    def handler(request):
        assert request.extensions["timeout"]["read"] == 37.0
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    configured = adapter()
    configured.settings.llm_timeout_seconds = 37
    client = httpx.AsyncClient
    with patch(
        "app.llm.httpx.AsyncClient",
        side_effect=lambda **kwargs: client(
            transport=httpx.MockTransport(handler), **kwargs
        ),
    ):
        asyncio.run(
            configured.language_task("Identify language only.", {"text": "Kumusta"})
        )

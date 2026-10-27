"""UMC outages must not be misreported as missing identities."""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from fastapi import WebSocketDisconnect

from app.api import make_router


@pytest.mark.parametrize("status,code", [(401, "umc_token_rejected"), (403, "umc_token_rejected"), (502, "umc_upstream_unavailable")])
def test_auth_checks_http_status_before_reading_identity(status, code):
    router = make_router(SimpleNamespace())
    endpoint = next(route.endpoint for route in router.routes if route.path == "/api/v1/ws")
    class Socket:
        scope = {"query_string": b"userId=fixture-user"}
        headers = {}
        sent = []
        received = False

        async def accept(self):
            pass

        async def receive_json(self):
            if self.received:
                raise WebSocketDisconnect()
            self.received = True
            return {"type": "auth", "umctoken": "fixture-token"}

        async def send_json(self, payload):
            self.sent.append(payload)

    socket = Socket()
    original = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(status, json={"data": {"id": "fixture-user"}}))
    settings = SimpleNamespace(umc_document_service_base_url="https://portal.test", umc_login_timeout_seconds=1)
    with patch("app.api.get_settings", return_value=settings), patch(
        "app.api.httpx.AsyncClient", side_effect=lambda **kwargs: original(transport=transport, **kwargs),
    ):
        asyncio.run(endpoint(socket))
    assert socket.sent == [{"type": "error", "code": code}]

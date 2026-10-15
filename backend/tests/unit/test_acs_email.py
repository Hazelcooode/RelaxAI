"""Azure Communication Services Email adapter: signing and error mapping (T04)."""

from __future__ import annotations

import base64
import hashlib
import hmac

import httpx
import orjson
import pytest
import respx

from app.platform.email import AcsEmailSender, EmailMessage
from app.platform.errors import UpstreamError

KEY = base64.b64encode(b"k" * 32).decode()
CONNECTION = f"endpoint=https://relaxai.europe.communication.azure.com/;accesskey={KEY}"
URL = "https://relaxai.europe.communication.azure.com/emails:send?api-version=2023-03-31"
MESSAGE = EmailMessage(to="avery.chen@relaxai-consulting.test", subject="Hi", text="Body")


async def test_request_is_signed_and_shaped_for_acs() -> None:
    with respx.mock() as router:
        route = router.post(URL).respond(status_code=202, json={"id": "x"})
        async with httpx.AsyncClient() as http:
            sender = AcsEmailSender(http, connection_string=CONNECTION, sender="no-reply@x.test")
            await sender.send(MESSAGE)
    request = route.calls.last.request
    body = orjson.loads(request.content)
    assert body == {
        "senderAddress": "no-reply@x.test",
        "recipients": {"to": [{"address": MESSAGE.to}]},
        "content": {"subject": "Hi", "plainText": "Body"},
    }
    content_hash = base64.b64encode(hashlib.sha256(request.content).digest()).decode()
    assert request.headers["x-ms-content-sha256"] == content_hash
    to_sign = (
        "POST\n/emails:send?api-version=2023-03-31\n"
        f"{request.headers['x-ms-date']};relaxai.europe.communication.azure.com;{content_hash}"
    )
    signature = base64.b64encode(
        hmac.new(base64.b64decode(KEY), to_sign.encode(), hashlib.sha256).digest()
    ).decode()
    assert request.headers["Authorization"] == (
        f"HMAC-SHA256 SignedHeaders=x-ms-date;host;x-ms-content-sha256&Signature={signature}"
    )


@pytest.mark.parametrize("status", [400, 429, 500, 503])
async def test_provider_errors_become_upstream_errors(status: int) -> None:
    with respx.mock() as router:
        router.post(URL).respond(status_code=status, json={"error": {"message": MESSAGE.to}})
        async with httpx.AsyncClient() as http:
            sender = AcsEmailSender(http, connection_string=CONNECTION, sender="no-reply@x.test")
            with pytest.raises(UpstreamError) as caught:
                await sender.send(MESSAGE)
    assert MESSAGE.to not in str(caught.value.details)


def test_connection_string_is_validated() -> None:
    with pytest.raises(ValueError, match="connection string"):
        AcsEmailSender(
            httpx.AsyncClient(), connection_string="endpoint=http://x;accesskey=", sender="a"
        )

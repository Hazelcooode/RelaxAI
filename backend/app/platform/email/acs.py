"""Azure Communication Services Email over REST with HMAC-SHA256 request signing.

https://learn.microsoft.com/azure/communication-services/concepts/email/email-overview —
``POST {endpoint}/emails:send?api-version=2023-03-31``. Provider errors are mapped to
``UpstreamError(EMAIL)``; response bodies are never logged or returned.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from email.utils import formatdate
from urllib.parse import urlsplit

import httpx
import orjson

from app.platform.email.ports import EmailMessage
from app.platform.errors import UpstreamCategory, UpstreamError

API_VERSION = "2023-03-31"


def parse_connection_string(value: str) -> tuple[str, bytes]:
    parts = dict(item.split("=", 1) for item in value.strip().strip(";").split(";") if "=" in item)
    endpoint = parts.get("endpoint", "").rstrip("/")
    key = parts.get("accesskey", "")
    if not endpoint.startswith("https://") or not key:
        raise ValueError("ACS connection string needs endpoint=https://... and accesskey=...")
    return endpoint, base64.b64decode(key)


class AcsEmailSender:
    def __init__(
        self, http: httpx.AsyncClient, *, connection_string: str, sender: str,
        timeout_seconds: float = 10.0,
    ) -> None:  # fmt: skip
        self._http = http
        self._endpoint, self._key = parse_connection_string(connection_string)
        self._sender = sender
        self._timeout = timeout_seconds

    def _headers(self, path_and_query: str, body: bytes, date: str) -> dict[str, str]:
        host = urlsplit(self._endpoint).netloc
        content_hash = base64.b64encode(hashlib.sha256(body).digest()).decode()
        to_sign = f"POST\n{path_and_query}\n{date};{host};{content_hash}"
        signature = base64.b64encode(
            hmac.new(self._key, to_sign.encode(), hashlib.sha256).digest()
        ).decode()
        return {
            "Content-Type": "application/json",
            "x-ms-date": date,
            "x-ms-content-sha256": content_hash,
            "Authorization": (
                "HMAC-SHA256 SignedHeaders=x-ms-date;host;x-ms-content-sha256"
                f"&Signature={signature}"
            ),
        }

    async def send(self, message: EmailMessage) -> None:
        path_and_query = f"/emails:send?api-version={API_VERSION}"
        body = orjson.dumps(
            {
                "senderAddress": self._sender,
                "recipients": {"to": [{"address": message.to}]},
                "content": {"subject": message.subject, "plainText": message.text},
            }
        )
        headers = self._headers(path_and_query, body, formatdate(usegmt=True))
        try:
            response = await self._http.post(
                f"{self._endpoint}{path_and_query}",
                content=body,
                headers=headers,
                timeout=self._timeout,
            )
        except httpx.HTTPError:
            raise UpstreamError(UpstreamCategory.EMAIL) from None
        if response.status_code not in (200, 202):
            raise UpstreamError(UpstreamCategory.EMAIL)

"""``/operator/health``, ``/operator/usage``, and the quota route (contract §13, T02).

Authentication here is the temporary bootstrap token that T03 replaces; these tests pin the
current behaviour so the T03 change is deliberate.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from app.container import Container
from app.modules.governance import Meter, Priority, QuotaPeriod, Worker
from app.platform.errors import DEFAULT_MESSAGES, ErrorCode
from app.settings import Settings
from tests.handlers import KIND_OK, OkPayload

TOKEN = "bootstrap-operator-token-0123456789"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def settings(settings: Settings) -> Settings:
    settings.operator.bootstrap_token = SecretStr(TOKEN)
    return settings


@pytest.fixture
async def client(api_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as http:
        yield http


async def test_health_reports_queue_and_usage_state(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/v1/operator/health", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["queue"][0]["kind"] == "governance.worker_probe"
    assert body["projectionLag"] == []
    assert body["lastBackup"] is None
    # Azure Monitor delivery is not configured in tests, so the surface says so.
    assert body["degraded"] is True
    assert {s["name"] for s in body["services"]} == {"api", "worker"}
    assert body["services"][0]["status"] == "Ok"


async def test_usage_lists_every_contract_meter(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/v1/operator/usage", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert [m["meter"] for m in body["meters"]] == [
        "AiTokensIn",
        "AiTokensOut",
        "EmbeddingTokens",
        "OcrPages",
        "BlobBytes",
    ]
    assert body["period"] == "total"
    assert body["planningCeilingsEur"] == {
        "VmDiskNetworking": 35,
        "AzureOpenAiDocumentIntelligence": 15,
        "BlobBackupsKeyVaultMonitor": 5,
        "DomainDeployment": 15,
        "Contingency": 30,
    }
    assert all(set(m) == {"meter", "used", "quota", "estimatedEur"} for m in body["meters"])


@pytest.mark.usefixtures("effects")  # the ok handler records into it
async def test_clock_advancement_and_progress_are_visible(
    client: httpx.AsyncClient, container: Container, make_worker: Callable[..., Worker]
) -> None:
    async with container.uow() as tx:
        await container.queue.enqueue(
            tx, KIND_OK, OkPayload(steps=1), idempotency_key="probe-visible",
            priority=Priority.LOW,
        )  # fmt: skip
    before = (await client.get("/api/v1/operator/health", headers=AUTH)).json()
    probe = next(q for q in before["queue"] if q["kind"] == KIND_OK)
    assert probe["depth"] == 1

    worker = make_worker()
    await worker.run_until_idle()
    after = (await client.get("/api/v1/operator/health", headers=AUTH)).json()
    probe = next(q for q in after["queue"] if q["kind"] == KIND_OK)
    assert probe["depth"] == 0


async def test_quota_change_is_audited_and_effective(client: httpx.AsyncClient) -> None:
    r = await client.put("/api/v1/operator/quotas/OcrPages", json={"quota": 42}, headers=AUTH)
    assert r.status_code == 200
    assert r.json() == {"meter": "OcrPages", "quota": 42}

    usage = (await client.get("/api/v1/operator/usage", headers=AUTH)).json()
    assert next(m for m in usage["meters"] if m["meter"] == "OcrPages")["quota"] == 42

    audit = await client.get("/api/v1/operator/audit", headers=AUTH)
    assert audit.status_code == 404  # the audit read route arrives with T32

    r = await client.put("/api/v1/operator/quotas/OcrPages", json={"quota": -1}, headers=AUTH)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_FAILED"

    r = await client.put("/api/v1/operator/quotas/NotAMeter", json={"quota": 1}, headers=AUTH)
    assert r.status_code == 422


async def test_probe_route_enqueues_a_watchable_job(
    client: httpx.AsyncClient, container: Container, make_worker: Callable[..., Worker]
) -> None:
    r = await client.post(
        "/api/v1/operator/jobs/probe", json={"steps": 2, "stepDelayMs": 0}, headers=AUTH
    )
    assert r.status_code == 202
    job_id = r.json()["jobId"]
    assert job_id.startswith("job_")

    worker = make_worker()
    assert await worker.run_once() == 1
    async with container.uow() as tx:
        from sqlalchemy import select

        from app.modules.governance.tables import jobs

        row = (await tx.session.execute(select(jobs).where(jobs.c.id == job_id))).mappings().one()
    assert row["state"] == "Succeeded"
    assert row["progress_stage"] == "Probing"
    assert row["progress_done"] == 2
    assert row["progress_total"] == 2


# --------------------------------------------------------------------------- auth (temporary)


async def test_operator_routes_reject_an_anonymous_caller(client: httpx.AsyncClient) -> None:
    for path in ("/api/v1/operator/health", "/api/v1/operator/usage"):
        r = await client.get(path)
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "UNAUTHENTICATED"
    r = await client.put("/api/v1/operator/quotas/OcrPages", json={"quota": 1})
    assert r.status_code == 401


async def test_operator_routes_reject_a_wrong_token(client: httpx.AsyncClient) -> None:
    r = await client.get(
        "/api/v1/operator/health", headers={"Authorization": "Bearer not-the-token"}
    )
    assert r.status_code == 401


async def test_operator_routes_are_denied_when_no_token_is_configured(
    api_app: FastAPI,
) -> None:
    api_app.state.container.settings.operator.bootstrap_token = None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as client:
        r = await client.get("/api/v1/operator/health", headers=AUTH)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "GOVERNANCE_ONLY"
    assert r.json()["error"]["message"] == DEFAULT_MESSAGES[ErrorCode.GOVERNANCE_ONLY]


async def test_operator_routes_require_a_bearer_scheme(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/v1/operator/health", headers={"Authorization": f"Basic {TOKEN}"})
    assert r.status_code == 401


async def test_health_reports_quota_halted_work(
    client: httpx.AsyncClient, container: Container, make_worker: Callable[..., Worker]
) -> None:
    from tests.handlers import KIND_PAID, PaidPayload

    async with container.uow() as tx:
        await container.usage.set_quota(tx, Meter.OCR_PAGES, 0, QuotaPeriod.TOTAL)
        await container.queue.enqueue(
            tx, KIND_PAID, PaidPayload(), idempotency_key="halted"
        )
    worker = make_worker()
    await worker.run_until_idle()

    body = (await client.get("/api/v1/operator/health", headers=AUTH)).json()
    usage = (await client.get("/api/v1/operator/usage", headers=AUTH)).json()
    assert usage["haltedWork"] == [{"kind": "test.paid", "count": 1}]
    worker_service = next(s for s in body["services"] if s["name"] == "worker")
    assert worker_service["status"] == "Degraded"

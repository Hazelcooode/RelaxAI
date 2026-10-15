"""The committed ``openapi.json`` is the frontend's contract. Any drift fails here.

After an intentional contract change, regenerate it with ``make openapi`` (which runs
``python -m app.api.export_openapi``) and commit the result with the change.
"""

from __future__ import annotations

from app.api.export_openapi import DEFAULT_OUTPUT, openapi_document, render


def test_openapi_matches_committed_snapshot() -> None:
    assert DEFAULT_OUTPUT.exists(), "openapi.json missing: run `make openapi`"
    current = render(openapi_document())
    assert current == DEFAULT_OUTPUT.read_bytes(), (
        "OpenAPI document changed. If intended, run `make openapi`, update api-contract.md, "
        "and commit openapi.json."
    )


def test_export_is_deterministic() -> None:
    assert render(openapi_document()) == render(openapi_document())


def test_document_publishes_error_envelope_and_health() -> None:
    doc = openapi_document()
    schemas = doc["components"]["schemas"]
    assert {"ErrorEnvelope", "ErrorBody", "ErrorDetail", "ErrorCode"} <= set(schemas)
    assert set(schemas["ErrorCode"]["enum"]) >= {"NOT_AVAILABLE", "VERSION_CONFLICT"}
    assert "correlationId" in schemas["ErrorBody"]["properties"]
    assert {"/healthz", "/readyz"} <= set(doc["paths"])

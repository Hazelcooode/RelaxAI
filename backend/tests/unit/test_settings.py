from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.settings import Environment, Settings, load_settings


def test_nested_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RELAXAI__DB__URL", "postgresql+psycopg://u:p@db:5432/x")
    monkeypatch.setenv("RELAXAI__DB__POOL_SIZE", "3")
    monkeypatch.setenv("RELAXAI__FIRM_ID", "frm_01J9Z3Q8M8V3W7G2E1N4R5T6Y7")
    monkeypatch.setenv("RELAXAI__LIMITS__PAGE_MAX_LIMIT", "50")
    settings = Settings()
    assert settings.db.url.get_secret_value() == "postgresql+psycopg://u:p@db:5432/x"
    assert settings.db.pool_size == 3
    assert settings.firm_id == "frm_01J9Z3Q8M8V3W7G2E1N4R5T6Y7"
    assert settings.limits.page_max_limit == 50


def test_secrets_are_not_rendered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RELAXAI__DB__URL", "postgresql+psycopg://u:hunter2@db/x")
    settings = Settings()
    assert "hunter2" not in repr(settings)
    assert "hunter2" not in str(settings.model_dump())


def test_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / "relaxai.env"
    env_file.write_text("RELAXAI__ENVIRONMENT=test\nRELAXAI__QDRANT__URL=http://q:6333\n")
    monkeypatch.setenv("RELAXAI_ENV_FILE", str(env_file))
    settings = load_settings()
    assert settings.environment is Environment.TEST
    assert settings.qdrant.url == "http://q:6333"


def test_production_rejects_default_cursor_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RELAXAI__ENVIRONMENT", "production")
    with pytest.raises(ValidationError, match="cursor_signing_key"):
        Settings()
    monkeypatch.setenv("RELAXAI__CRYPTO__CURSOR_SIGNING_KEY", "x" * 40)
    with pytest.raises(ValidationError, match="entra"):
        Settings()
    monkeypatch.setenv("RELAXAI__ENTRA__AUTHORITY", "https://login.test/tenant/v2.0")
    monkeypatch.setenv("RELAXAI__ENTRA__AUDIENCE", "api://relaxai")
    monkeypatch.setenv("RELAXAI__ENTRA__ALLOWED_TENANT_IDS", '["tenant"]')
    monkeypatch.setenv("RELAXAI__ENTRA__OPERATOR_SUBJECTS", '["sub-ops"]')
    settings = Settings()
    assert settings.environment is Environment.PRODUCTION
    assert settings.entra.operator_subjects == ["sub-ops"]


def test_firm_id_must_be_prefixed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RELAXAI__FIRM_ID", "acme")
    with pytest.raises(ValidationError):
        Settings()

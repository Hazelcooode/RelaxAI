from __future__ import annotations

import pytest
from typer.testing import CliRunner

from app import __version__
from app.cli import cli


def test_version_command() -> None:
    result = CliRunner().invoke(cli, ["version"])
    assert result.exit_code == 0
    assert result.output.strip() == __version__


class _FakeOrganization:
    def __init__(self) -> None:
        self.appointed: list[str] = []

    async def user_id_for_email(self, email: str) -> str:
        return f"usr_for_{email.split('@')[0]}"

    async def appoint_first_firm_manager(self, operator: object, user_id: str) -> str:
        from app.platform.errors import AlreadyExists

        if self.appointed:
            raise AlreadyExists()
        self.appointed.append(user_id)
        return "fmn_1"


class _FakeContainer:
    def __init__(self, organization: _FakeOrganization) -> None:
        self.organization = organization

    async def aclose(self) -> None:
        return None


def test_appoint_first_refuses_a_second_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.cli as cli_module

    organization = _FakeOrganization()
    monkeypatch.setattr(cli_module, "_container", lambda: _FakeContainer(organization))
    runner = CliRunner()
    first = runner.invoke(cli, ["firm-manager", "appoint-first", "--email", "sam@x.test"])
    assert first.exit_code == 0, first.output
    assert organization.appointed == ["usr_for_sam"]
    second = runner.invoke(cli, ["firm-manager", "appoint-first", "--email", "avery@x.test"])
    assert second.exit_code == 1
    assert "ALREADY_EXISTS" in second.output


def test_bootstrap_commands_are_registered() -> None:
    runner = CliRunner()
    for group, commands in (("domain", ("add", "remove", "list")), ("firm", ("init",))):
        result = runner.invoke(cli, [group, "--help"])
        assert result.exit_code == 0
        assert all(command in result.output for command in commands)

from __future__ import annotations

from typer.testing import CliRunner

from app import __version__
from app.cli import cli


def test_version_command() -> None:
    result = CliRunner().invoke(cli, ["version"])
    assert result.exit_code == 0
    assert result.output.strip() == __version__

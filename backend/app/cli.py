"""``relaxai-admin``: platform operator commands. T02 and later add commands."""

from __future__ import annotations

import typer

from app import __version__

cli = typer.Typer(name="relaxai-admin", no_args_is_help=True, add_completion=False)


@cli.callback()
def _root() -> None:
    """RelaxAI platform operator commands."""


@cli.command()
def version() -> None:
    """Print the backend version."""
    typer.echo(__version__)


def main() -> None:
    cli()


if __name__ == "__main__":
    main()

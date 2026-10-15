"""Write the OpenAPI document deterministically: ``python -m app.api.export_openapi``.

The output depends only on code (never on environment or settings), with sorted keys and a
trailing newline, so regenerating without a contract change produces a byte-identical file.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import orjson

from app.container import build_container
from app.main_api import create_api_app
from app.settings import Settings

DEFAULT_OUTPUT = Path(__file__).resolve().parents[2] / "openapi.json"


def openapi_document() -> dict[str, Any]:
    # Defaults only (no env file, no env vars): the engine is created lazily and never used.
    settings = Settings.model_construct()
    return create_api_app(build_container(settings)).openapi()


def render(document: dict[str, Any]) -> bytes:
    return orjson.dumps(document, option=orjson.OPT_SORT_KEYS | orjson.OPT_INDENT_2) + b"\n"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-o", "--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    output: Path = args.output
    output.write_bytes(render(openapi_document()))
    print(f"wrote {output}")  # noqa: T201


if __name__ == "__main__":
    main()

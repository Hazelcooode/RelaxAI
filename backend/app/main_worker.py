"""Worker process entry point: ``python -m app.main_worker`` (systemd ExecStart).

Runs the claim loop and scheduler, serves no HTTP, and shuts down gracefully on SIGTERM or
SIGINT: stop claiming, let in-flight jobs finish within the grace period, then release their
leases (the attempt is not counted).
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import signal
from typing import Any

from app.container import Container, build_container
from app.platform.logging import configure_logging
from app.settings import Settings, load_settings

log = logging.getLogger("relaxai.worker")

# Every module's handlers must be imported before the worker starts, so the registry is
# complete. Later tasks add their module here.
JOB_MODULES = ("app.modules.governance.jobs", "app.modules.actions.jobs")


def register_jobs(modules: tuple[str, ...] = JOB_MODULES) -> None:
    for module in modules:
        importlib.import_module(module)


async def run_worker(container: Container, *, stop: asyncio.Event | None = None) -> None:
    """Run until ``stop`` is set (or a signal arrives)."""
    register_jobs()
    container.registry.validate()
    stopper = stop or asyncio.Event()
    try:
        await container.worker.run(stopper)
    finally:
        await container.aclose()


def _install_signal_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - not available on Windows
            signal.signal(sig, lambda *_: stop.set())


async def _main(settings: Settings) -> None:
    configure_logging(settings.telemetry.log_level)
    container = build_container(settings)
    stop = asyncio.Event()
    _install_signal_handlers(stop)
    await run_worker(container, stop=stop)


def main(argv: list[str] | None = None) -> Any:
    del argv
    asyncio.run(_main(load_settings()))


if __name__ == "__main__":
    main()

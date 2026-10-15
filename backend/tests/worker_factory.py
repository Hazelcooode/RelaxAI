"""Build a ``Worker`` over any container, with deterministic jitter for tests."""

from __future__ import annotations

import random
from typing import Any

from app.container import Container
from app.modules.governance import Worker


def worker_for(
    container: Container,
    *,
    owner: str = "worker-1",
    concurrency: int = 1,
    seed: int = 7,
    kinds: list[str] | None = None,
    poll_interval: float = 0.01,
    shutdown_grace: float = 30.0,
    **kwargs: Any,
) -> Worker:
    return Worker(
        store=container.store,
        queue=container.queue,
        usage=container.usage,
        registry=container.registry,
        clock=container.clock,
        uow=container.uow,
        concurrency=concurrency,
        poll_interval=poll_interval,
        shutdown_grace=shutdown_grace,
        owner=owner,
        kinds=kinds,
        rng=random.Random(seed),  # noqa: S311 - jitter only
        services=container.services,
        **kwargs,
    )

"""``relaxai-admin``: Platform Operator commands.

These are the bootstrap path for actions an operator must be able to take without the API
(PRD §13), and the local way to watch the worker loop work.
"""

from __future__ import annotations

import asyncio
import sys
import time
import uuid
from collections.abc import Coroutine

import typer

from app import __version__
from app.container import Container, build_container
from app.main_worker import register_jobs
from app.modules.governance import (
    Meter,
    Priority,
    WorkerProbePayload,
)
from app.modules.governance.jobs import WORKER_PROBE
from app.modules.organization_access import OperatorActor
from app.platform.errors import AppError
from app.platform.logging import configure_logging
from app.settings import load_settings

cli = typer.Typer(name="relaxai-admin", no_args_is_help=True, add_completion=False)
jobs_cli = typer.Typer(name="jobs", help="Job queue commands")
usage_cli = typer.Typer(name="usage", help="Usage and quota commands")
firm_cli = typer.Typer(name="firm", help="Firm bootstrap")
domain_cli = typer.Typer(name="domain", help="Approved Team Member email domains")
firm_manager_cli = typer.Typer(name="firm-manager", help="Firm Manager bootstrap")
cli.add_typer(jobs_cli)
cli.add_typer(usage_cli)
cli.add_typer(firm_cli)
cli.add_typer(domain_cli)
cli.add_typer(firm_manager_cli)


@cli.callback()
def _root() -> None:
    """RelaxAI Platform Operator commands."""


@cli.command()
def version() -> None:
    """Print the backend version."""
    typer.echo(__version__)


def _container() -> Container:
    settings = load_settings()
    configure_logging(settings.telemetry.log_level)
    register_jobs()
    return build_container(settings)


def _run(coro: Coroutine[object, object, None]) -> None:
    asyncio.run(coro)


def _fail(error: AppError) -> None:
    typer.echo(f"error: {error.code.value}: {error.message}", err=True)
    for detail in error.details:
        typer.echo(f"  {detail.field or '-'}: {detail.message}", err=True)
    raise typer.Exit(code=1)


@jobs_cli.command("stats")
def jobs_stats() -> None:
    """Queue depth, oldest age, and recent failures per job kind."""

    async def run() -> None:
        container = _container()
        try:
            for stat in await container.queue.stats():
                typer.echo(
                    f"{stat.kind:36} depth={stat.depth:<4} running={stat.running:<3} "
                    f"waiting={stat.waiting_for_user:<3} oldest={stat.oldest_age_seconds}s "
                    f"failed24h={stat.failed_24h}"
                )
        finally:
            await container.aclose()

    _run(run())


@jobs_cli.command("probe")
def jobs_probe(
    steps: int = typer.Option(3, help="Progress reports the probe job makes"),
    step_delay_ms: int = typer.Option(500, help="Delay between reports"),
    watch: bool = typer.Option(True, help="Poll the job until it finishes"),
    timeout: float = typer.Option(30.0, help="Seconds to wait when --watch is used"),
) -> None:
    """Enqueue a harmless job and watch a worker pick it up.

    Run the worker in another terminal first: ``python -m app.main_worker``.
    """

    async def run() -> None:
        from sqlalchemy import select

        from app.modules.governance.tables import jobs as jobs_table

        container = _container()
        try:
            async with container.uow() as tx:
                job_id = await container.queue.enqueue(
                    tx,
                    WORKER_PROBE,
                    WorkerProbePayload(steps=steps, step_delay_ms=step_delay_ms),
                    idempotency_key=f"probe:{uuid.uuid4().hex}",
                    priority=Priority.LOW,
                )
            typer.echo(f"job {job_id} enqueued")
            if not watch:
                return
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                async with container.uow() as tx:
                    row = (
                        (
                            await tx.session.execute(
                                select(jobs_table).where(jobs_table.c.id == job_id)
                            )
                        )
                        .mappings()
                        .one()
                    )
                typer.echo(
                    f"  state={row['state']} attempts={row['attempts']} "
                    f"stage={row['progress_stage']} {row['progress_done']}/{row['progress_total']}"
                    f" {row['progress_unit'] or ''}".rstrip()
                )
                if row["state"] in {"Succeeded", "Failed", "Cancelled"}:
                    return
                await asyncio.sleep(1)
            typer.echo("timed out waiting for the job; is a worker running?", err=True)
            sys.exit(1)
        finally:
            await container.aclose()

    _run(run())


@usage_cli.command("show")
def usage_show() -> None:
    """Meters, quotas, estimated cost, and halted work."""

    async def run() -> None:
        container = _container()
        try:
            report = await container.usage.usage()
            typer.echo(f"period {report.period}")
            for meter in report.meters:
                typer.echo(
                    f"  {meter.meter.value:18} used={meter.used:<12} quota={meter.quota:<12} "
                    f"reserved={meter.reserved:<8} estimatedEUR={meter.estimated_eur}"
                )
            for halted in report.halted_work:
                typer.echo(f"  halted: {halted.kind} x{halted.count}")
            total = sum(report.planning_ceilings_eur.values())
            typer.echo(f"planning ceilings total EUR {total}")
        finally:
            await container.aclose()

    _run(run())


@usage_cli.command("set-quota")
def usage_set_quota(meter: Meter, quota: int) -> None:
    """Set a firm's quota for one meter (persisted in ``governance.quota_policies``)."""

    async def run() -> None:
        container = _container()
        try:
            async with container.uow() as tx:
                await container.usage.set_quota(tx, meter, quota)
            typer.echo(f"{meter.value} quota set to {quota}")
        finally:
            await container.aclose()

    _run(run())


# --------------------------------------------------------------------------- identity (T03)
# Every command acts as the CLI Platform Operator and writes an audit event.


@firm_cli.command("init")
def firm_init(name: str = typer.Option(..., help="Firm display name")) -> None:
    """Create this stamp's firm row (idempotent)."""

    async def run() -> None:
        container = _container()
        try:
            firm_id = await container.organization.init_firm(OperatorActor.cli(), name)
            typer.echo(f"firm {firm_id} ready")
        except AppError as error:
            _fail(error)
        finally:
            await container.aclose()

    _run(run())


@domain_cli.command("add")
def domain_add(domain: str) -> None:
    """Approve an email domain for Team Member activation."""

    async def run() -> None:
        container = _container()
        try:
            await container.organization.add_approved_domain(OperatorActor.cli(), domain)
            typer.echo(f"{domain} approved")
        except AppError as error:
            _fail(error)
        finally:
            await container.aclose()

    _run(run())


@domain_cli.command("remove")
def domain_remove(domain: str) -> None:
    """Withdraw an approved domain (existing accounts stay active)."""

    async def run() -> None:
        container = _container()
        try:
            await container.organization.remove_approved_domain(OperatorActor.cli(), domain)
            typer.echo(f"{domain} removed")
        except AppError as error:
            _fail(error)
        finally:
            await container.aclose()

    _run(run())


@domain_cli.command("list")
def domain_list() -> None:
    """List approved domains."""

    async def run() -> None:
        container = _container()
        try:
            for item in await container.organization.list_approved_domains():
                typer.echo(f"{item.domain}  added {item.created_at.isoformat()}")
        finally:
            await container.aclose()

    _run(run())


@firm_manager_cli.command("appoint-first")
def firm_manager_appoint_first(
    email: str = typer.Option(..., help="Email of an activated Team Member"),
) -> None:
    """Appoint the first Firm Manager; refused once an active Firm Manager exists."""

    async def run() -> None:
        container = _container()
        try:
            user_id = await container.organization.user_id_for_email(email)
            assignment_id = await container.organization.appoint_first_firm_manager(
                OperatorActor.cli(), user_id
            )
            typer.echo(f"firm manager appointed ({assignment_id})")
        except AppError as error:
            _fail(error)
        finally:
            await container.aclose()

    _run(run())


def main() -> None:
    cli()


if __name__ == "__main__":
    main()

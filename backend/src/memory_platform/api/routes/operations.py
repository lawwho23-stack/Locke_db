"""Owner usage and grant-filtered background job status; no private content."""

from typing import Any
from uuid import UUID

from fastapi import APIRouter
from sqlalchemy import func, select

from memory_platform.api.deps import EngineDep, PrincipalDep, SettingsDep
from memory_platform.enums import ActorKind, Capability
from memory_platform.errors import AppError, ErrorCode
from memory_platform.knowledge_tables import knowledge_jobs, provider_usage, sources
from memory_platform.services.quotas import usage_snapshot
from memory_platform.tables import memories

router = APIRouter(prefix="/v1", tags=["operations"])


@router.get("/usage")
def usage(principal: PrincipalDep, engine: EngineDep, settings: SettingsDep) -> dict[str, Any]:
    if principal.actor_kind != ActorKind.owner or not principal.is_admin:
        raise AppError(ErrorCode.forbidden, "Owner administration required.")
    with engine.begin() as conn:
        resources = usage_snapshot(conn, principal.workspace_id, settings)
        measured = provider_usage.c.tokens.is_not(None)
        rows = (
            conn.execute(
                select(
                    func.count().label("requests"),
                    func.coalesce(func.sum(provider_usage.c.reserved_usd), 0).label("reserved_usd"),
                    func.coalesce(func.sum(provider_usage.c.charged_usd), 0).label("charged_usd"),
                    func.count().filter(provider_usage.c.completed_at.is_(None)).label("pending"),
                    func.count().filter(measured).label("measured_requests"),
                    func.count().filter(~measured).label("estimated_requests"),
                ).where(provider_usage.c.workspace_id == principal.workspace_id)
            )
            .mappings()
            .one()
        )
        jobs = conn.execute(
            select(knowledge_jobs.c.status, func.count())
            .where(knowledge_jobs.c.workspace_id == principal.workspace_id)
            .group_by(knowledge_jobs.c.status)
        ).all()
        pending_cleanup = conn.execute(
            select(func.count())
            .select_from(knowledge_jobs)
            .where(
                knowledge_jobs.c.workspace_id == principal.workspace_id,
                knowledge_jobs.c.kind == "source_cleanup",
                knowledge_jobs.c.status.in_(["queued", "leased", "failed"]),
            )
        ).scalar_one()
        return {
            "resources": resources,
            "provider": {
                **dict(rows),
                "daily_budget_usd": settings.provider_daily_budget_usd,
                "cost_basis": "configured_price_estimate",
            },
            "jobs": dict(jobs),
            "pending_cleanup": pending_cleanup,
        }


@router.get("/jobs/{job_id}")
def job(job_id: UUID, principal: PrincipalDep, engine: EngineDep) -> dict[str, Any]:
    with engine.begin() as conn:
        row = (
            conn.execute(
                select(knowledge_jobs).where(
                    knowledge_jobs.c.id == job_id,
                    knowledge_jobs.c.workspace_id == principal.workspace_id,
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise AppError(ErrorCode.not_found, "Not found.")
        table = memories if row["kind"] == "memory_embedding" else sources
        scope_id = conn.execute(
            select(table.c.scope_id).where(
                table.c.id == row["target_id"],
                table.c.workspace_id == principal.workspace_id,
            )
        ).scalar_one_or_none()
        if scope_id is None:
            raise AppError(ErrorCode.not_found, "Not found.")
        principal.require(scope_id, Capability.memory_read)
        # Never reveal lease tokens, deduplication keys or provider request payloads.
        fields = (
            "id",
            "kind",
            "target_id",
            "target_version",
            "status",
            "attempts",
            "available_at",
            "error_code",
            "created_at",
            "finished_at",
        )
        return {field: row[field] for field in fields}

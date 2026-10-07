"""Document sources and authorised grounded recall."""

from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Query
from sqlalchemy import select

from memory_platform.api.deps import EngineDep, PrincipalDep, SettingsDep
from memory_platform.api.knowledge_schemas import (
    RecallRequest,
    RecallResponse,
    SourceReplaceRequest,
    SourceUploadRequest,
)
from memory_platform.cache import cached_recall
from memory_platform.enums import Capability
from memory_platform.knowledge_tables import source_versions, sources
from memory_platform.providers import configured_provider
from memory_platform.services.access import readable_scope_ids
from memory_platform.services.sources import checked_source, delete_source, upload_source
from memory_platform.storage import LocalStorage

router = APIRouter(prefix="/v1", tags=["knowledge"])


@router.post("/recall", response_model=RecallResponse)
def recall_context(
    req: RecallRequest, principal: PrincipalDep, engine: EngineDep, settings: SettingsDep
) -> RecallResponse:
    return cached_recall(engine, principal, req, settings, configured_provider(engine, settings))


@router.post("/sources", status_code=202)
def create_source(
    req: SourceUploadRequest, principal: PrincipalDep, engine: EngineDep, settings: SettingsDep
) -> dict[str, Any]:
    principal.require(req.scope_id, Capability.source_ingest)
    storage = LocalStorage(getattr(settings, "storage_dir", Path(".data/sources")))
    return upload_source(
        engine,
        principal,
        storage,
        scope_id=req.scope_id,
        title=req.title,
        filename=req.filename,
        encoded=req.content_base64,
        settings=settings,
    )


@router.get("/sources")
def list_sources(
    principal: PrincipalDep,
    engine: EngineDep,
    scope_id: UUID | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=100000),
) -> dict[str, Any]:
    ids = readable_scope_ids(principal, [scope_id] if scope_id else None)
    with engine.begin() as conn:
        rows = (
            conn.execute(
                select(
                    sources.c.id,
                    sources.c.scope_id,
                    sources.c.title,
                    sources.c.current_version,
                    source_versions.c.status,
                    sources.c.created_at,
                )
                .select_from(
                    sources.join(
                        source_versions,
                        (sources.c.id == source_versions.c.source_id)
                        & (sources.c.current_version == source_versions.c.version),
                    )
                )
                .where(
                    sources.c.workspace_id == principal.workspace_id,
                    sources.c.scope_id.in_(ids),
                    sources.c.deleted_at.is_(None),
                )
                .order_by(sources.c.created_at.desc(), sources.c.id)
                .limit(limit)
                .offset(offset)
            )
            .mappings()
            .all()
        )
        return {"items": [dict(row) for row in rows], "offset": offset, "limit": limit}


@router.get("/sources/{source_id}")
def get_source(source_id: UUID, principal: PrincipalDep, engine: EngineDep) -> dict[str, Any]:
    with engine.begin() as conn:
        row = checked_source(conn, principal, source_id, Capability.memory_read)
        versions = (
            conn.execute(
                select(
                    source_versions.c.id,
                    source_versions.c.version,
                    source_versions.c.filename,
                    source_versions.c.format,
                    source_versions.c.byte_size,
                    source_versions.c.status,
                    source_versions.c.summary,
                    source_versions.c.error_code,
                    source_versions.c.created_at,
                )
                .where(source_versions.c.source_id == source_id)
                .order_by(source_versions.c.version)
            )
            .mappings()
            .all()
        )
        return {**dict(row), "versions": [dict(version) for version in versions]}


@router.put("/sources/{source_id}", status_code=202)
def replace_source(
    source_id: UUID,
    req: SourceReplaceRequest,
    principal: PrincipalDep,
    engine: EngineDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    with engine.begin() as conn:
        row = checked_source(conn, principal, source_id, Capability.source_ingest)
    return upload_source(
        engine,
        principal,
        LocalStorage(getattr(settings, "storage_dir", Path(".data/sources"))),
        scope_id=row["scope_id"],
        title=row["title"],
        filename=req.filename,
        encoded=req.content_base64,
        source_id=source_id,
        expected_version=req.expected_version,
        settings=settings,
    )


@router.delete("/sources/{source_id}")
def remove_source(
    source_id: UUID, principal: PrincipalDep, engine: EngineDep, settings: SettingsDep
) -> dict[str, Any]:
    return delete_source(
        engine,
        principal,
        LocalStorage(getattr(settings, "storage_dir", Path(".data/sources"))),
        source_id,
    )

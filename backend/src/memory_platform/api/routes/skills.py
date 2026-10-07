"""Approved skill HTTP interfaces; no drafts and no code execution."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query

from memory_platform.api.deps import EngineDep, PrincipalDep, RequestIdDep, SettingsDep
from memory_platform.api.skill_schemas import (
    SkillImportRequest,
    SkillListResponse,
    SkillOut,
    SkillPackageOut,
    SkillRevisionRequest,
    SkillRollbackRequest,
)
from memory_platform.services.skills import (
    change_skill,
    download_skill,
    import_skill,
    list_skills,
    resolve_skill,
)

router = APIRouter(prefix="/v1/skills", tags=["skills"])


@router.post("", response_model=SkillOut, status_code=201)
def store(
    req: SkillImportRequest,
    principal: PrincipalDep,
    engine: EngineDep,
    request_id: RequestIdDep,
    settings: SettingsDep,
) -> SkillOut:
    with engine.begin() as conn:
        return import_skill(conn, principal, req, request_id=request_id, settings=settings)


@router.get("", response_model=SkillListResponse)
def listing(
    principal: PrincipalDep, engine: EngineDep, scope_id: UUID | None = None
) -> SkillListResponse:
    with engine.begin() as conn:
        return list_skills(conn, principal, scope_id)


@router.get("/resolve", response_model=SkillPackageOut)
def resolve(
    scope_id: UUID,
    command: Annotated[str, Query(pattern=r"^/[a-z][a-z0-9_]{0,63}$")],
    principal: PrincipalDep,
    engine: EngineDep,
) -> SkillPackageOut:
    with engine.begin() as conn:
        return resolve_skill(conn, principal, scope_id, command)


@router.get("/{skill_id}/versions/{version}", response_model=SkillPackageOut)
def download(
    skill_id: UUID, version: int, principal: PrincipalDep, engine: EngineDep
) -> SkillPackageOut:
    with engine.begin() as conn:
        return download_skill(conn, principal, skill_id, version)


@router.post("/{skill_id}/rollback", response_model=SkillOut)
def rollback(
    skill_id: UUID,
    req: SkillRollbackRequest,
    principal: PrincipalDep,
    engine: EngineDep,
    request_id: RequestIdDep,
) -> SkillOut:
    with engine.begin() as conn:
        return change_skill(
            conn,
            principal,
            skill_id,
            req.expected_revision,
            version=req.version,
            request_id=request_id,
        )


@router.post("/{skill_id}/revoke", response_model=SkillOut)
def revoke(
    skill_id: UUID,
    req: SkillRevisionRequest,
    principal: PrincipalDep,
    engine: EngineDep,
    request_id: RequestIdDep,
) -> SkillOut:
    with engine.begin() as conn:
        return change_skill(
            conn, principal, skill_id, req.expected_revision, version=None, request_id=request_id
        )

"""Owner-approved atomic import, exact lookup, and version lifecycle."""

import base64
import hashlib
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from memory_platform.api.skill_schemas import (
    SkillFile,
    SkillImportRequest,
    SkillListResponse,
    SkillOut,
    SkillPackageOut,
)
from memory_platform.auth.principal import Principal
from memory_platform.config import Settings
from memory_platform.enums import ActorKind, Capability
from memory_platform.errors import AppError, ErrorCode
from memory_platform.services.idempotency import record_activity
from memory_platform.skill_package import package_hash, validate_package
from memory_platform.skill_tables import skill_files, skill_versions, skills
from memory_platform.tables import scopes


def require_owner(principal: Principal, scope_id: UUID) -> None:
    if principal.actor_kind != ActorKind.owner or not principal.is_admin:
        raise AppError(ErrorCode.forbidden, "Owner admin approval required.")
    principal.require(scope_id, Capability.skill_write)


def _out(conn: Connection, row: Any) -> SkillOut:
    hash_value = conn.execute(
        select(skill_versions.c.package_hash).where(
            skill_versions.c.skill_id == row["id"],
            skill_versions.c.version == row["active_version"],
        )
    ).scalar_one_or_none()
    return SkillOut(
        id=row["id"],
        scope_id=row["scope_id"],
        command=row["command"],
        revision=row["revision"],
        active_version=row["active_version"],
        package_hash=hash_value,
        revoked=row["revoked"],
    )


def _target(conn: Connection, principal: Principal, skill_id: UUID, *, write: bool = False) -> Any:
    query = select(skills).where(
        skills.c.id == skill_id, skills.c.workspace_id == principal.workspace_id
    )
    if write:
        query = query.with_for_update()
    row = conn.execute(query).mappings().first()
    if row is None:
        raise AppError(ErrorCode.not_found, "Not found.")
    if write:
        require_owner(principal, row["scope_id"])
    else:
        principal.require(row["scope_id"], Capability.skill_read)
        if row["revoked"]:
            raise AppError(ErrorCode.not_found, "Not found.")
    return row


def import_skill(
    conn: Connection,
    principal: Principal,
    req: SkillImportRequest,
    *,
    request_id: str,
    settings: Settings | None = None,
) -> SkillOut:
    require_owner(principal, req.scope_id)
    scope = conn.execute(
        select(scopes.c.id).where(
            scopes.c.id == req.scope_id, scopes.c.workspace_id == principal.workspace_id
        )
    ).scalar_one_or_none()
    if scope is None:
        raise AppError(ErrorCode.not_found, "Not found.")
    try:
        files = validate_package(req.files)
        computed_hash = package_hash(req.files)
    except ValueError as exc:
        raise AppError(ErrorCode.validation_error, "Invalid skill package.") from exc
    if computed_hash != req.approved_package_hash:
        raise AppError(ErrorCode.validation_error, "Package does not match owner approval.")
    if settings is not None:
        from memory_platform.services.quotas import enforce_quota

        enforce_quota(
            conn,
            principal.workspace_id,
            settings,
            "skill_bytes",
            additional=sum(len(file.data) for file in files),
        )
    # Lock the registry key, including its absent state, without taking scope-row locks.
    # Rollback/revoke only lock the skill row, so no reverse scope/skill lock order exists.
    lock_key = int.from_bytes(
        hashlib.sha256(
            f"skill-command:{principal.workspace_id}:{req.scope_id}:{req.command}".encode()
        ).digest()[:8],
        "big",
        signed=True,
    )
    conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})
    row = (
        conn.execute(
            select(skills)
            .where(skills.c.scope_id == req.scope_id, skills.c.command == req.command)
            .with_for_update()
        )
        .mappings()
        .first()
    )
    if row is None:
        if req.expected_revision != 0:
            raise AppError(ErrorCode.version_conflict, "Skill revision changed.")
        row = (
            conn.execute(
                pg_insert(skills)
                .values(
                    workspace_id=principal.workspace_id, scope_id=req.scope_id, command=req.command
                )
                .returning(skills)
            )
            .mappings()
            .one()
        )
        version, revision = 1, 1
    else:
        if row["revision"] != req.expected_revision:
            raise AppError(ErrorCode.version_conflict, "Skill revision changed.")
        latest = conn.scalar(
            select(func.max(skill_versions.c.version)).where(skill_versions.c.skill_id == row["id"])
        )
        version, revision = int(latest or 0) + 1, row["revision"] + 1
    conn.execute(
        insert(skill_versions).values(
            skill_id=row["id"],
            workspace_id=principal.workspace_id,
            version=version,
            package_hash=computed_hash,
            approved_by=principal.actor_id,
        )
    )
    conn.execute(
        insert(skill_files),
        [
            {
                "skill_id": row["id"],
                "workspace_id": principal.workspace_id,
                "version": version,
                "path": file.path,
                "content": file.data,
                "sha256": file.sha256,
                "executable": file.executable,
            }
            for file in files
        ],
    )
    row = (
        conn.execute(
            update(skills)
            .where(skills.c.id == row["id"])
            .values(active_version=version, revision=revision, revoked=False)
            .returning(skills)
        )
        .mappings()
        .one()
    )
    record_activity(
        conn,
        workspace_id=principal.workspace_id,
        actor_id=principal.actor_id,
        action="skill.import",
        target_ids=(row["id"],),
        scope_id=req.scope_id,
        request_id=request_id,
        result="ok",
    )
    return _out(conn, row)


def list_skills(conn: Connection, principal: Principal, scope_id: UUID | None) -> SkillListResponse:
    if scope_id is not None:
        principal.require(scope_id, Capability.skill_read)
    ids = [scope_id] if scope_id is not None else list(principal.scopes_with(Capability.skill_read))
    rows = conn.execute(
        select(skills)
        .where(
            skills.c.workspace_id == principal.workspace_id,
            skills.c.scope_id.in_(ids),
            skills.c.revoked.is_(False),
        )
        .order_by(skills.c.command, skills.c.id)
        .limit(100)
    ).mappings()
    return SkillListResponse(items=[_out(conn, row) for row in rows])


def download_skill(
    conn: Connection, principal: Principal, skill_id: UUID, version: int | None = None
) -> SkillPackageOut:
    row = _target(conn, principal, skill_id)
    selected = version if version is not None else row["active_version"]
    approval = (
        conn.execute(
            select(skill_versions).where(
                skill_versions.c.skill_id == skill_id, skill_versions.c.version == selected
            )
        )
        .mappings()
        .first()
    )
    if approval is None:
        raise AppError(ErrorCode.not_found, "Not found.")
    files = conn.execute(
        select(skill_files)
        .where(skill_files.c.skill_id == skill_id, skill_files.c.version == selected)
        .order_by(skill_files.c.path)
    ).mappings()
    return SkillPackageOut(
        **_out(conn, row).model_dump(exclude={"package_hash"}),
        package_hash=approval["package_hash"],
        version=selected,
        approved_by=approval["approved_by"],
        approved_at=approval["approved_at"],
        files=[
            SkillFile(
                path=file["path"],
                content_base64=base64.b64encode(file["content"]).decode(),
                executable=file["executable"],
            )
            for file in files
        ],
    )


def resolve_skill(
    conn: Connection, principal: Principal, scope_id: UUID, command: str
) -> SkillPackageOut:
    principal.require(scope_id, Capability.skill_read)
    skill_id = conn.scalar(
        select(skills.c.id).where(
            skills.c.scope_id == scope_id,
            skills.c.workspace_id == principal.workspace_id,
            skills.c.command == command,
            skills.c.revoked.is_(False),
        )
    )
    if skill_id is None:
        raise AppError(ErrorCode.not_found, "Not found.")
    return download_skill(conn, principal, skill_id)


def change_skill(
    conn: Connection,
    principal: Principal,
    skill_id: UUID,
    expected_revision: int,
    *,
    version: int | None,
    request_id: str,
) -> SkillOut:
    row = _target(conn, principal, skill_id, write=True)
    if row["revision"] != expected_revision:
        raise AppError(ErrorCode.version_conflict, "Skill revision changed.")
    if (
        version is not None
        and conn.scalar(
            select(skill_versions.c.version).where(
                skill_versions.c.skill_id == skill_id, skill_versions.c.version == version
            )
        )
        is None
    ):
        raise AppError(ErrorCode.not_found, "Approved version not found.")
    updated = (
        conn.execute(
            update(skills)
            .where(skills.c.id == skill_id)
            .values(
                revision=row["revision"] + 1,
                active_version=version if version is not None else row["active_version"],
                revoked=version is None,
            )
            .returning(skills)
        )
        .mappings()
        .one()
    )
    record_activity(
        conn,
        workspace_id=principal.workspace_id,
        actor_id=principal.actor_id,
        action="skill.rollback" if version is not None else "skill.revoke",
        target_ids=(skill_id,),
        scope_id=row["scope_id"],
        request_id=request_id,
        result="ok",
    )
    return _out(conn, updated)

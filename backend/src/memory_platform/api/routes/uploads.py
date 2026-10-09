"""Authorized direct-upload receipts; finalization never accepts a remote URL."""

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, insert, select, update

from memory_platform.api.deps import EngineDep, PrincipalDep, SettingsDep
from memory_platform.enums import Capability
from memory_platform.errors import AppError, ErrorCode
from memory_platform.services.sources import checked_source, decode_upload, upload_source
from memory_platform.storage import blob_path, configured_storage
from memory_platform.tables import scopes, workspaces
from memory_platform.upload_tables import upload_receipts

router = APIRouter(prefix="/v1/uploads", tags=["uploads"])


class UploadIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope_id: UUID
    title: str = Field(min_length=1, max_length=300)
    filename: str = Field(min_length=1, max_length=255)
    byte_size: int = Field(ge=1, le=20 * 1024 * 1024)
    source_id: UUID | None = None
    expected_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def replacement_pair(self) -> "UploadIntent":
        if (self.source_id is None) != (self.expected_version is None):
            raise ValueError("Replacement requires source_id and expected_version together.")
        return self


def receipt(conn: Any, principal: Any, upload_id: UUID, *, lock: bool = False) -> Any:
    query = select(upload_receipts).where(
        upload_receipts.c.id == upload_id,
        upload_receipts.c.workspace_id == principal.workspace_id,
        upload_receipts.c.credential_id == principal.credential_id,
    )
    row = conn.execute(query.with_for_update() if lock else query).mappings().first()
    if row is None:
        raise AppError(ErrorCode.not_found, "Upload not found.")
    principal.require(row["scope_id"], Capability.source_ingest)
    if row["expires_at"] <= datetime.now(UTC) and row["result"] is None:
        raise AppError(ErrorCode.invalid_transition, "Upload expired. Start a new upload.")
    return row


@router.post("", status_code=201)
def authorize_upload(
    req: UploadIntent, principal: PrincipalDep, engine: EngineDep
) -> dict[str, Any]:
    principal.require(req.scope_id, Capability.source_ingest)
    decode_upload(req.filename, base64.b64encode(b"format check").decode())
    with engine.begin() as conn:
        conn.execute(
            select(workspaces.c.id)
            .where(workspaces.c.id == principal.workspace_id)
            .with_for_update()
        )
        pending = conn.execute(
            select(func.count())
            .select_from(upload_receipts)
            .where(
                upload_receipts.c.workspace_id == principal.workspace_id,
                upload_receipts.c.result.is_(None),
                upload_receipts.c.expires_at > datetime.now(UTC),
            )
        ).scalar_one()
        if pending >= 20:
            raise AppError(ErrorCode.quota_exceeded, "Too many pending uploads. Try again later.")
        if (
            conn.execute(
                select(scopes.c.id).where(
                    scopes.c.id == req.scope_id, scopes.c.workspace_id == principal.workspace_id
                )
            ).first()
            is None
        ):
            raise AppError(ErrorCode.not_found, "Not found.")
        if req.source_id is not None:
            source = checked_source(conn, principal, req.source_id, Capability.source_ingest)
            if (
                source["scope_id"] != req.scope_id
                or source["current_version"] != req.expected_version
            ):
                raise AppError(ErrorCode.version_conflict, "Source version changed.")
        row = (
            conn.execute(
                insert(upload_receipts)
                .values(
                    workspace_id=principal.workspace_id,
                    credential_id=principal.credential_id,
                    **req.model_dump(),
                    expires_at=datetime.now(UTC) + timedelta(minutes=15),
                )
                .returning(upload_receipts)
            )
            .mappings()
            .one()
        )
        return {
            "id": row["id"],
            "pathname": blob_path(str(row["id"])),
            "expires_at": row["expires_at"],
            "byte_size": row["byte_size"],
        }


@router.get("/{upload_id}")
def get_upload(
    upload_id: UUID, principal: PrincipalDep, engine: EngineDep, settings: SettingsDep
) -> dict[str, Any]:
    with engine.begin() as conn:
        row = receipt(conn, principal, upload_id)
        if row["result"] is not None:
            raise AppError(ErrorCode.invalid_transition, "Upload has already completed.")
        return {
            "id": row["id"],
            "pathname": blob_path(str(row["id"])),
            "expires_at": row["expires_at"],
            "byte_size": row["byte_size"],
            "storage_provider": settings.storage_provider,
        }


@router.post("/{upload_id}/finalize", status_code=202)
def finalize_upload(
    upload_id: UUID, principal: PrincipalDep, engine: EngineDep, settings: SettingsDep
) -> dict[str, Any]:
    storage = configured_storage(settings)
    with engine.begin() as conn:
        row = receipt(conn, principal, upload_id, lock=True)
        if row["result"] is not None:
            return dict(row["result"])
        try:
            content = storage.get(str(upload_id))
        except FileNotFoundError:
            raise AppError(
                ErrorCode.invalid_transition,
                "Upload is incomplete. Retry after the file finishes uploading.",
            ) from None
        except Exception:
            raise AppError(
                ErrorCode.dependency_unavailable, "Private storage is unavailable."
            ) from None
        if len(content) != row["byte_size"]:
            raise AppError(
                ErrorCode.validation_error, "Uploaded file size does not match the receipt."
            )
        result = upload_source(
            engine,
            principal,
            storage,
            scope_id=row["scope_id"],
            title=row["title"],
            filename=row["filename"],
            content=content,
            stored_version_id=upload_id,
            connection=conn,
            source_id=row["source_id"],
            expected_version=row["expected_version"],
            settings=settings,
        )
        result = json.loads(json.dumps(result, default=str))
        conn.execute(
            update(upload_receipts).where(upload_receipts.c.id == upload_id).values(result=result)
        )
        return dict(result)

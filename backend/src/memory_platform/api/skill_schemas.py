"""Exact command and approved-package transport models."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Command = Annotated[str, StringConstraints(pattern=r"^/[a-z][a-z0-9_]{0,63}$")]
PackageHash = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class SkillFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=1024)
    content_base64: str = Field(max_length=1398104)
    executable: bool = False


class SkillImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope_id: UUID
    command: Command
    files: list[SkillFile] = Field(min_length=1, max_length=128)
    approved_package_hash: PackageHash
    expected_revision: int = Field(default=0, ge=0)


class SkillRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)


class SkillRollbackRequest(SkillRevisionRequest):
    version: int = Field(ge=1)


class SkillOut(BaseModel):
    id: UUID
    scope_id: UUID
    command: str
    revision: int
    active_version: int | None
    package_hash: str | None
    revoked: bool


class SkillPackageOut(SkillOut):
    version: int
    files: list[SkillFile]
    approved_by: UUID
    approved_at: datetime


class SkillListResponse(BaseModel):
    items: list[SkillOut]

"""Memory HTTP routes: authorize, transact, call services, return safe responses."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from sqlalchemy import Connection, select

from memory_platform.api.deps import (
    EngineDep,
    IdempotencyKeyDep,
    PrincipalDep,
    RequestIdDep,
    SettingsDep,
)
from memory_platform.api.schemas import (
    ForgetResponse,
    MemoryDetail,
    MemoryListResponse,
    MemoryWriteResponse,
    RememberRequest,
    UpdateRequest,
)
from memory_platform.auth.principal import Principal
from memory_platform.enums import Capability, MemoryState, MemoryType
from memory_platform.errors import AppError, ErrorCode
from memory_platform.services.forget import forget_memory
from memory_platform.services.idempotency import run_write
from memory_platform.services.memory_queries import get_memory, list_memories
from memory_platform.services.remember import remember
from memory_platform.services.update import update_memory
from memory_platform.tables import memories, scopes

router = APIRouter(prefix="/v1/memories", tags=["memories"])


def _authorize_target(
    conn: Connection, principal: Principal, memory_id: UUID, cap: Capability
) -> None:
    # Include tombstones so a DELETE can replay, but check today's grants before replay.
    scope_id = conn.execute(
        select(memories.c.scope_id).where(
            memories.c.id == memory_id, memories.c.workspace_id == principal.workspace_id
        )
    ).scalar_one_or_none()
    if scope_id is None:
        raise AppError(ErrorCode.not_found, "Not found.")
    principal.require(scope_id, cap)


@router.post("", response_model=MemoryWriteResponse, status_code=201)
def create_memory(
    req: RememberRequest,
    principal: PrincipalDep,
    engine: EngineDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
    key: IdempotencyKeyDep,
) -> JSONResponse:
    with engine.begin() as conn:
        principal.require(req.scope_id, Capability.memory_write)
        if (
            conn.execute(
                select(scopes.c.id).where(
                    scopes.c.id == req.scope_id, scopes.c.workspace_id == principal.workspace_id
                )
            ).first()
            is None
        ):
            raise AppError(ErrorCode.not_found, "Not found.")
        outcome = run_write(
            conn,
            principal,
            operation="memory.remember",
            idempotency_key=key,
            payload=req.model_dump(mode="json"),
            request_id=request_id,
            ttl_hours=settings.idempotency_ttl_hours,
            action=lambda: remember(
                conn,
                principal,
                req,
                request_id=request_id,
                hmac_key=settings.hmac_key_bytes,
                settings=settings,
            ),
        )
    return JSONResponse(outcome.body, status_code=outcome.status_code)


@router.get("", response_model=MemoryListResponse)
def read_memories(
    principal: PrincipalDep,
    engine: EngineDep,
    scope_id: Annotated[list[UUID] | None, Query()] = None,
    label: str | None = None,
    type: MemoryType | None = None,
    state: Literal["active", "draft", "superseded", "expired"] = "active",
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: str | None = None,
    q: Annotated[str | None, Query(max_length=1000)] = None,
) -> MemoryListResponse:
    with engine.begin() as conn:
        return list_memories(
            conn,
            principal,
            scope_ids=scope_id,
            label=label,
            mtype=type,
            state=MemoryState(state),
            limit=limit,
            cursor=cursor,
            q=q,
        )


@router.get("/{memory_id}", response_model=MemoryDetail)
def read_memory(memory_id: UUID, principal: PrincipalDep, engine: EngineDep) -> MemoryDetail:
    with engine.begin() as conn:
        return get_memory(conn, principal, memory_id)


@router.patch("/{memory_id}", response_model=MemoryWriteResponse)
def change_memory(
    memory_id: UUID,
    req: UpdateRequest,
    principal: PrincipalDep,
    engine: EngineDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
    key: IdempotencyKeyDep,
) -> JSONResponse:
    with engine.begin() as conn:
        _authorize_target(conn, principal, memory_id, Capability.memory_write)
        outcome = run_write(
            conn,
            principal,
            operation="memory.update",
            idempotency_key=key,
            payload={
                "memory_id": str(memory_id),
                **req.model_dump(mode="json", exclude_unset=True),
            },
            request_id=request_id,
            ttl_hours=settings.idempotency_ttl_hours,
            action=lambda: update_memory(
                conn,
                principal,
                memory_id,
                req,
                request_id=request_id,
                hmac_key=settings.hmac_key_bytes,
            ),
        )
    return JSONResponse(outcome.body, status_code=outcome.status_code)


@router.delete("/{memory_id}", response_model=ForgetResponse)
def delete_memory(
    memory_id: UUID,
    principal: PrincipalDep,
    engine: EngineDep,
    settings: SettingsDep,
    request_id: RequestIdDep,
    key: IdempotencyKeyDep,
) -> JSONResponse:
    with engine.begin() as conn:
        _authorize_target(conn, principal, memory_id, Capability.memory_delete)
        outcome = run_write(
            conn,
            principal,
            operation="memory.forget",
            idempotency_key=key,
            payload={"memory_id": str(memory_id)},
            request_id=request_id,
            ttl_hours=settings.idempotency_ttl_hours,
            action=lambda: forget_memory(
                conn, principal, memory_id, request_id=request_id, hmac_key=settings.hmac_key_bytes
            ),
        )
    return JSONResponse(outcome.body, status_code=outcome.status_code)

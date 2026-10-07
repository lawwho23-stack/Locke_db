"""Project-scoped tasks and credential-bound session lifecycle."""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query

from memory_platform.api.deps import EngineDep, PrincipalDep, SettingsDep
from memory_platform.api.task_schemas import (
    CheckpointRequest,
    EventRequest,
    HandoffRequest,
    SessionRequest,
    TaskCreateRequest,
)
from memory_platform.services import tasks as service

router = APIRouter(prefix="/v1", tags=["tasks"])


@router.post("/sessions", status_code=201)
def register_session(
    req: SessionRequest, principal: PrincipalDep, engine: EngineDep
) -> dict[str, Any]:
    with engine.begin() as conn:
        return service.register_session(conn, principal, req)


@router.get("/sessions")
def sessions(
    principal: PrincipalDep,
    engine: EngineDep,
    scope_id: UUID,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> dict[str, Any]:
    with engine.begin() as conn:
        return {"items": service.list_sessions(conn, principal, scope_id, limit)}


@router.get("/sessions/{session_id}")
def session(session_id: UUID, principal: PrincipalDep, engine: EngineDep) -> dict[str, Any]:
    with engine.begin() as conn:
        return service.session_detail(conn, principal, session_id)


@router.post("/sessions/{session_id}/heartbeat")
def heartbeat(session_id: UUID, principal: PrincipalDep, engine: EngineDep) -> dict[str, Any]:
    with engine.begin() as conn:
        return service.session_detail(conn, principal, session_id, heartbeat=True)


@router.post("/tasks", status_code=201)
def create_task(
    req: TaskCreateRequest, principal: PrincipalDep, engine: EngineDep, settings: SettingsDep
) -> dict[str, Any]:
    with engine.begin() as conn:
        return service.create_task(conn, principal, req, settings=settings)


@router.get("/tasks")
def tasks(
    principal: PrincipalDep,
    engine: EngineDep,
    scope_id: UUID,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> dict[str, Any]:
    with engine.begin() as conn:
        return {"items": service.list_tasks(conn, principal, scope_id, limit)}


@router.get("/tasks/{task_id}")
def task(task_id: UUID, principal: PrincipalDep, engine: EngineDep) -> dict[str, Any]:
    with engine.begin() as conn:
        return service.read_task(conn, principal, task_id)


@router.get("/tasks/{task_id}/events")
def events(
    task_id: UUID,
    principal: PrincipalDep,
    engine: EngineDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> dict[str, Any]:
    with engine.begin() as conn:
        return {"items": service.list_events(conn, principal, task_id, limit)}


@router.patch("/tasks/{task_id}/checkpoint")
def checkpoint(
    task_id: UUID, req: CheckpointRequest, principal: PrincipalDep, engine: EngineDep
) -> dict[str, Any]:
    with engine.begin() as conn:
        return service.write_event(conn, principal, task_id, req, "checkpoint")


@router.post("/tasks/{task_id}/handoff")
def handoff(
    task_id: UUID, req: HandoffRequest, principal: PrincipalDep, engine: EngineDep
) -> dict[str, Any]:
    with engine.begin() as conn:
        return service.write_event(conn, principal, task_id, req, "handoff")


@router.post("/tasks/{task_id}/accept-handoff")
def accept_handoff(
    task_id: UUID, req: EventRequest, principal: PrincipalDep, engine: EngineDep
) -> dict[str, Any]:
    with engine.begin() as conn:
        return service.write_event(conn, principal, task_id, req, "accept_handoff")


@router.post("/tasks/{task_id}/recover")
def recover(
    task_id: UUID, req: EventRequest, principal: PrincipalDep, engine: EngineDep
) -> dict[str, Any]:
    with engine.begin() as conn:
        return service.write_event(conn, principal, task_id, req, "recover")


@router.delete("/tasks/{task_id}")
def delete(
    task_id: UUID, req: EventRequest, principal: PrincipalDep, engine: EngineDep
) -> dict[str, Any]:
    with engine.begin() as conn:
        return service.write_event(conn, principal, task_id, req, "delete")

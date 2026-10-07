"""Explicit selected event and session summary interfaces."""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query

from memory_platform.api.deps import EngineDep, PrincipalDep
from memory_platform.services import capture as service
from memory_platform.services.capture import SelectedEvent, SessionStateRequest

router = APIRouter(prefix="/v1/sessions", tags=["capture"])


@router.post("/{session_id}/events", status_code=201)
def append_event(
    session_id: UUID, req: SelectedEvent, principal: PrincipalDep, engine: EngineDep
) -> dict[str, Any]:
    with engine.begin() as conn:
        return service.append_event(conn, principal, session_id, req)


@router.get("/{session_id}/events")
def events(
    session_id: UUID,
    principal: PrincipalDep,
    engine: EngineDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> dict[str, Any]:
    with engine.begin() as conn:
        return {"items": service.list_events(conn, principal, session_id, limit)}


@router.get("/{session_id}/state")
def state(session_id: UUID, principal: PrincipalDep, engine: EngineDep) -> dict[str, Any]:
    with engine.begin() as conn:
        return service.session_state(conn, principal, session_id)


@router.patch("/{session_id}/state")
def update_state(
    session_id: UUID, req: SessionStateRequest, principal: PrincipalDep, engine: EngineDep
) -> dict[str, Any]:
    with engine.begin() as conn:
        return service.update_state(conn, principal, session_id, req)

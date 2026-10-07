"""Owner dashboard metadata endpoints."""

from typing import Any

from fastapi import APIRouter

from memory_platform.api.deps import EngineDep, PrincipalDep
from memory_platform.services import dashboard

router = APIRouter(prefix="/v1/dashboard", tags=["dashboard"])


@router.get("/summary")
def summary(principal: PrincipalDep, engine: EngineDep) -> dict[str, Any]:
    with engine.begin() as conn:
        return dashboard.summary(conn, principal)


@router.get("/activity")
def activity(principal: PrincipalDep, engine: EngineDep) -> dict[str, Any]:
    with engine.begin() as conn:
        return dashboard.activity_feed(conn, principal)


@router.get("/graph")
def graph(principal: PrincipalDep, engine: EngineDep) -> dict[str, Any]:
    with engine.begin() as conn:
        return dashboard.graph(conn, principal)


@router.get("/connections")
def connections(principal: PrincipalDep, engine: EngineDep) -> dict[str, Any]:
    with engine.begin() as conn:
        return dashboard.connections(conn, principal)

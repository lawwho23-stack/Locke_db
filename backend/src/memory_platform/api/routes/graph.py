"""Bounded graph navigation and owner-created relationships."""

from typing import Any, Literal, Self
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, model_validator

from memory_platform.api.deps import EngineDep, PrincipalDep, RequestIdDep
from memory_platform.services import graph

router = APIRouter(prefix="/v1", tags=["graph"])


class RelationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    from_id: UUID
    to_id: UUID

    @model_validator(mode="after")
    def distinct(self) -> Self:
        if self.from_id == self.to_id:
            raise ValueError("A relationship requires two distinct memories.")
        return self


@router.get("/graph")
def get_graph(
    principal: PrincipalDep,
    engine: EngineDep,
    scope_id: UUID | None = None,
    q: str = Query(default="", max_length=200),
    kind: Literal["scope", "memory", "source", "task", "session"] | None = None,
    focus_id: UUID | None = None,
    cursor: str | None = Query(default=None, max_length=512),
) -> dict[str, Any]:
    with engine.begin() as conn:
        return graph.graph(
            conn, principal, scope_id=scope_id, query=q, kind=kind, focus_id=focus_id, cursor=cursor
        )


@router.post("/relations", status_code=201)
def add_relation(
    req: RelationRequest, principal: PrincipalDep, engine: EngineDep, request_id: RequestIdDep
) -> dict[str, Any]:
    with engine.begin() as conn:
        return graph.create_relation(conn, principal, req.from_id, req.to_id, request_id=request_id)


@router.delete("/relations/{relation_id}")
def remove_relation(
    relation_id: UUID, principal: PrincipalDep, engine: EngineDep, request_id: RequestIdDep
) -> dict[str, Any]:
    with engine.begin() as conn:
        return graph.delete_relation(conn, principal, relation_id, request_id=request_id)

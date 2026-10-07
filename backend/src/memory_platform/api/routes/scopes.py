"""Scope listing and owner administration."""

from fastapi import APIRouter

from memory_platform.api.deps import EngineDep, PrincipalDep, RequestIdDep
from memory_platform.api.schemas import ScopeCreateRequest, ScopeListResponse, ScopeOut
from memory_platform.services.scopes import create_scope, list_scopes

router = APIRouter(prefix="/v1/scopes", tags=["scopes"])


@router.get("", response_model=ScopeListResponse)
def read_scopes(principal: PrincipalDep, engine: EngineDep) -> ScopeListResponse:
    with engine.begin() as conn:
        return list_scopes(conn, principal)


@router.post("", response_model=ScopeOut, status_code=201)
def add_scope(
    req: ScopeCreateRequest, principal: PrincipalDep, engine: EngineDep, request_id: RequestIdDep
) -> ScopeOut:
    with engine.begin() as conn:
        return create_scope(conn, principal, req, request_id=request_id)

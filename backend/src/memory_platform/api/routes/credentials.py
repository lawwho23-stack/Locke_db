"""Issue and revoke scoped credentials; plaintext tokens never enter replay storage."""

from uuid import UUID

from fastapi import APIRouter

from memory_platform.api.deps import EngineDep, PrincipalDep, RequestIdDep
from memory_platform.api.schemas import (
    CredentialCreateRequest,
    CredentialCreateResponse,
    CredentialRevokeResponse,
)
from memory_platform.services.credentials import create_credential, revoke_credential

router = APIRouter(prefix="/v1/credentials", tags=["credentials"])


@router.post("", response_model=CredentialCreateResponse, status_code=201)
def issue_credential(
    req: CredentialCreateRequest,
    principal: PrincipalDep,
    engine: EngineDep,
    request_id: RequestIdDep,
) -> CredentialCreateResponse:
    with engine.begin() as conn:
        return create_credential(conn, principal, req, request_id=request_id)


@router.delete("/{credential_id}", response_model=CredentialRevokeResponse)
def revoke(
    credential_id: UUID, principal: PrincipalDep, engine: EngineDep, request_id: RequestIdDep
) -> CredentialRevokeResponse:
    with engine.begin() as conn:
        return revoke_credential(conn, principal, credential_id, request_id=request_id)

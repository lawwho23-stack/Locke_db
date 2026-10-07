"""HTTP update/cursor edge cases, beyond the direct service happy path."""

import base64
import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from tests.conftest import WorkspaceInfo


@pytest.mark.parametrize("invalid", [42, {}, [], None, True])
def test_cursor_uuid_must_be_string(owner_client: TestClient, invalid: object) -> None:
    cursor = base64.urlsafe_b64encode(
        json.dumps({"u": datetime.now(UTC).isoformat(), "i": invalid}).encode()
    ).decode()
    response = owner_client.get("/v1/memories", params={"cursor": cursor})
    assert response.status_code == 400 and response.json()["error"]["code"] == "bad_request"


def test_update_clears_optional_fields_and_validates_window(
    owner_client: TestClient, workspace: WorkspaceInfo
) -> None:
    req = {
        "scope_id": str(workspace.personal_scope_id),
        "type": "fact",
        "content": "window",
        "fact_key": "window",
        "valid_from": "2030-01-01T00:00:00Z",
        "valid_until": "2030-02-01T00:00:00Z",
    }
    mid = owner_client.post("/v1/memories", json=req).json()["id"]
    invalid = owner_client.patch(
        f"/v1/memories/{mid}", json={"expected_version": 1, "valid_until": "2029-01-01T00:00:00Z"}
    )
    assert invalid.status_code == 422
    assert (
        owner_client.patch(f"/v1/memories/{mid}", json={"expected_version": 1}).status_code == 422
    )
    assert (
        owner_client.patch(
            f"/v1/memories/{mid}", json={"expected_version": 1, "state": "active"}
        ).status_code
        == 409
    )
    result = owner_client.patch(
        f"/v1/memories/{mid}", json={"expected_version": 1, "valid_until": None, "fact_key": None}
    )
    assert result.status_code == 200 and result.json()["version"] == 2
    detail = owner_client.get(f"/v1/memories/{mid}").json()
    assert detail["fact_key"] is None and detail["valid_until"] is None

"""Owner approval, exact lookups and immutable version lifecycle on real PostgreSQL."""

import base64

from sqlalchemy import func, select


def payload(workspace, data=b"# approved\r\n", revision=0):
    from memory_platform.skill_package import package_hash

    files = [
        {"path": "SKILL.md", "content_base64": base64.b64encode(data).decode(), "executable": False}
    ]
    return {
        "scope_id": str(workspace.personal_scope_id),
        "command": "/github_review",
        "files": files,
        "approved_package_hash": package_hash(files),
        "expected_revision": revision,
    }


def test_owner_approval_version_lifecycle(owner_client, workspace):
    initial = owner_client.post("/v1/skills", json=payload(workspace))
    assert initial.status_code == 201, initial.text
    old = initial.json()
    response = owner_client.get(
        "/v1/skills/resolve",
        params={"scope_id": str(workspace.personal_scope_id), "command": "/github_review"},
    )
    assert response.status_code == 200
    assert base64.b64decode(response.json()["files"][0]["content_base64"]) == b"# approved\r\n"
    updated = owner_client.post("/v1/skills", json=payload(workspace, b"# next", 1))
    assert updated.status_code == 201
    assert updated.json()["active_version"] == 2
    conflict = owner_client.post("/v1/skills", json=payload(workspace, b"# stale", 1))
    assert conflict.status_code == 409
    rolled = owner_client.post(
        f"/v1/skills/{old['id']}/rollback", json={"version": 1, "expected_revision": 2}
    )
    assert rolled.status_code == 200
    assert rolled.json()["package_hash"] == old["package_hash"]
    revoked = owner_client.post(f"/v1/skills/{old['id']}/revoke", json={"expected_revision": 3})
    assert revoked.status_code == 200
    assert owner_client.get(f"/v1/skills/{old['id']}/versions/1").status_code == 404


def test_false_approval_persists_nothing(owner_client, workspace, engine):
    from memory_platform.skill_tables import skills

    req = payload(workspace)
    req["approved_package_hash"] = "0" * 64
    assert owner_client.post("/v1/skills", json=req).status_code == 422
    with engine.connect() as conn:
        assert (
            conn.scalar(
                select(func.count())
                .select_from(skills)
                .where(skills.c.workspace_id == workspace.id)
            )
            == 0
        )


def test_command_lookup_is_exact(owner_client, workspace):
    assert owner_client.post("/v1/skills", json=payload(workspace)).status_code == 201
    for command in ("/github", "/github-review", "/Github_review"):
        result = owner_client.get(
            "/v1/skills/resolve",
            params={"scope_id": str(workspace.personal_scope_id), "command": command},
        )
        assert result.status_code in (404, 422)


def test_agent_cannot_import_even_with_write_grant(make_agent_client, workspace, engine):
    from memory_platform.skill_tables import skills

    agent = make_agent_client([workspace.personal_scope_id], ["skill:read", "skill:write"])
    req = payload(workspace)
    assert agent.post("/v1/skills", json=req).status_code == 403
    req["approved"] = True
    assert agent.post("/v1/skills", json=req).status_code in (403, 422)
    with engine.connect() as conn:
        assert (
            conn.scalar(
                select(func.count())
                .select_from(skills)
                .where(skills.c.workspace_id == workspace.id)
            )
            == 0
        )


def test_skill_read_grant_does_not_grant_personal_memory(
    owner_client, workspace, make_scope, make_agent_client
):
    shared = make_scope("project", "shared-skills")
    request = payload(workspace)
    request["scope_id"] = str(shared)
    assert owner_client.post("/v1/skills", json=request).status_code == 201
    agent = make_agent_client([shared], ["skill:read"])
    assert (
        agent.get(
            "/v1/skills/resolve", params={"scope_id": str(shared), "command": "/github_review"}
        ).status_code
        == 200
    )
    assert (
        agent.get("/v1/memories", params={"scope_id": str(workspace.personal_scope_id)}).status_code
        == 404
    )


def test_approved_version_database_rows_cannot_change(owner_client, workspace, engine):
    import pytest
    from sqlalchemy import update
    from sqlalchemy.exc import DBAPIError

    from memory_platform.skill_tables import skill_files

    created = owner_client.post("/v1/skills", json=payload(workspace)).json()
    with pytest.raises(DBAPIError), engine.begin() as conn:
        conn.execute(
            update(skill_files)
            .where(skill_files.c.skill_id == created["id"])
            .values(content=b"changed")
        )


def test_concurrent_first_import_has_one_winner(owner_client, workspace, client_for_token):
    from concurrent.futures import ThreadPoolExecutor

    req = payload(workspace)

    def send(_):
        with client_for_token(owner_client.token) as client:
            response = client.post("/v1/skills", json=req)
            return response.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(send, range(2)))
    assert sorted(statuses) == [201, 409]

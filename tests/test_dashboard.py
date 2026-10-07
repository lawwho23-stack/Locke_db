"""Dashboard owner authorization, grant filtering and graph bounds."""

import importlib

import pytest
from sqlalchemy import insert

from memory_platform.auth.principal import resolve_principal
from memory_platform.errors import AppError
from memory_platform.tables import activity, memory_relations


def service():
    return importlib.import_module("memory_platform.services.dashboard")


def test_dashboard_rejects_agent_with_all_read_grants(engine, workspace, make_agent_client):
    agent = make_agent_client(
        [workspace.personal_scope_id], ["memory:read", "task:read", "skill:read"]
    )
    with engine.begin() as conn:
        principal = resolve_principal(conn, agent.token)
        with pytest.raises(AppError) as error:
            service().summary(conn, principal)
    assert error.value.http_status == 403


def test_graph_excludes_hidden_scope_and_labels_edge_provenance(
    engine, workspace, owner_client, make_scope
):
    hidden = make_scope("project", "hidden-dashboard")
    visible_ids = []
    for content in ("Visible first", "Visible second"):
        response = owner_client.post(
            "/v1/memories",
            json={"scope_id": str(workspace.personal_scope_id), "type": "fact", "content": content},
        )
        assert response.status_code == 201
        visible_ids.append(response.json()["id"])
    hidden_response = owner_client.post(
        "/v1/memories", json={"scope_id": str(hidden), "type": "fact", "content": "Hidden"}
    )
    from sqlalchemy import delete

    from memory_platform.tables import credential_grants

    with engine.begin() as conn:
        conn.execute(
            delete(credential_grants).where(
                credential_grants.c.credential_id == workspace.owner_credential_id,
                credential_grants.c.scope_id == hidden,
            )
        )
        conn.execute(
            insert(memory_relations).values(
                workspace_id=workspace.id,
                from_id=visible_ids[0],
                to_id=visible_ids[1],
                type="related_to",
                origin="asserted",
                created_by=workspace.owner_actor_id,
            )
        )
        principal = resolve_principal(conn, workspace.owner_token)
        graph = service().graph(conn, principal)
        assert hidden_response.json()["id"] not in {str(node["id"]) for node in graph["nodes"]}
        assert len(graph["nodes"]) <= 100 and len(graph["edges"]) <= 200
        assert graph["edges"][0]["origin"] == "asserted"
        assert graph["edges"][0]["created_by"] == workspace.owner_actor_id
        counts = service().summary(conn, principal)
        assert counts["counts"]["memories"] == 2


def test_activity_has_no_hidden_scoped_events(engine, workspace, make_scope):
    hidden = make_scope("project", "hidden-activity")
    with engine.begin() as conn:
        from sqlalchemy import delete

        from memory_platform.tables import credential_grants

        conn.execute(
            delete(credential_grants).where(
                credential_grants.c.credential_id == workspace.owner_credential_id,
                credential_grants.c.scope_id == hidden,
            )
        )
        for scope_id in (workspace.personal_scope_id, hidden):
            conn.execute(
                insert(activity).values(
                    workspace_id=workspace.id,
                    actor_id=workspace.owner_actor_id,
                    action="safe.action",
                    scope_id=scope_id,
                    request_id="test",
                    result="ok",
                )
            )
        rows = service().activity_feed(conn, resolve_principal(conn, workspace.owner_token))[
            "items"
        ]
        assert len(rows) == 1
        assert rows[0]["scope_id"] == workspace.personal_scope_id


def test_connection_inventory_hides_credentials_secrets(engine, workspace, make_agent_client):
    agent = make_agent_client([workspace.personal_scope_id], ["memory:read"])
    with engine.begin() as conn:
        owner = resolve_principal(conn, workspace.owner_token)
        rows = service().connections(conn, owner)["items"]
        assert len(rows) == 1
        assert rows[0]["id"] == agent.credential_id
        assert set(rows[0]).isdisjoint({"token", "token_hash"})
        assert agent.token not in str(rows)
        with pytest.raises(AppError):
            service().connections(conn, resolve_principal(conn, agent.token))

"""Bounded graph projection and two-ended relation authorization."""

from sqlalchemy import select

from memory_platform.tables import memory_relations, scopes


def create(owner_client, scope, content):
    response = owner_client.post(
        "/v1/memories", json={"scope_id": str(scope), "type": "fact", "content": content}
    )
    assert response.status_code == 201
    return response.json()["id"]


def test_graph_relations_require_both_ends(
    engine, workspace, owner_client, make_agent_client, make_scope
):
    first = create(owner_client, workspace.personal_scope_id, "Graph visible")
    hidden_scope = make_scope("project", "graph-hidden")
    second = create(owner_client, hidden_scope, "Graph hidden")
    agent = make_agent_client([workspace.personal_scope_id], ["memory:read", "memory:write"])
    assert agent.post("/v1/relations", json={"from_id": first, "to_id": second}).status_code in (
        403,
        404,
    )
    response = owner_client.post("/v1/relations", json={"from_id": first, "to_id": second})
    assert response.status_code == 201
    relation = response.json()
    assert relation["origin"] == "asserted"
    assert agent.get("/v1/graph").status_code == 200
    graph = agent.get("/v1/graph").json()
    assert second not in {n["id"] for n in graph["nodes"]}
    assert not any(e["id"] == relation["id"] for e in graph["edges"])
    assert agent.delete("/v1/relations/" + relation["id"]).status_code in (403, 404)
    assert owner_client.delete("/v1/relations/" + relation["id"]).status_code == 200
    with engine.begin() as conn:
        assert (
            conn.scalar(
                select(memory_relations.c.status).where(memory_relations.c.id == relation["id"])
            )
            == "removed"
        )
        assert (
            conn.scalar(select(scopes.c.revision).where(scopes.c.id == workspace.personal_scope_id))
            > 1
        )


def test_graph_filter_focus_cursor_bounds(workspace, owner_client):
    first = create(owner_client, workspace.personal_scope_id, "Graph alpha")
    second = create(owner_client, workspace.personal_scope_id, "Graph beta")
    relation = owner_client.post("/v1/relations", json={"from_id": first, "to_id": second})
    assert relation.status_code == 201
    result = owner_client.get("/v1/graph", params={"kind": "memory", "q": "alpha"}).json()
    assert [n["id"] for n in result["nodes"]] == [first]
    focused = owner_client.get("/v1/graph", params={"focus_id": first}).json()
    assert {first, second}.issubset({n["id"] for n in focused["nodes"]})
    assert len(focused["nodes"]) <= 100 and len(focused["edges"]) <= 200
    assert owner_client.get("/v1/graph", params={"cursor": "bad"}).status_code == 400
    assert (
        owner_client.post("/v1/relations", json={"from_id": first, "to_id": first}).status_code
        == 422
    )

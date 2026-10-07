"""Real HTTP task grants, replay and writer serialization."""

from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import Engine, select

from memory_platform.task_tables import task_events, tasks
from tests.conftest import WorkspaceInfo


def _session(client: TestClient, scope_id: UUID, name: str = "one") -> str:
    response = client.post(
        "/v1/sessions", json={"scope_id": str(scope_id), "client": "test", "external_id": name}
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def _task(client: TestClient, scope_id: UUID, session_id: str) -> dict[str, object]:
    response = client.post(
        "/v1/tasks",
        json={
            "scope_id": str(scope_id),
            "session_id": session_id,
            "event_id": str(uuid4()),
            "sequence": 1,
            "title": "Task",
            "goal": "Goal",
        },
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


def test_http_scope_and_credential_boundaries(
    owner_client: TestClient,
    workspace: WorkspaceInfo,
    make_agent_client: Callable[[Sequence[UUID], Sequence[str]], TestClient],
) -> None:
    session = _session(owner_client, workspace.personal_scope_id)
    task = _task(owner_client, workspace.personal_scope_id, session)
    hidden = make_agent_client([], [])
    reader = make_agent_client([workspace.personal_scope_id], ["task:read"])
    writer = make_agent_client([workspace.personal_scope_id], ["task:read", "task:write"])
    assert hidden.get(f"/v1/tasks/{task['task_id']}").status_code == 404
    assert reader.get(f"/v1/tasks/{task['task_id']}").status_code == 200
    assert (
        reader.post(
            "/v1/sessions",
            json={"scope_id": str(workspace.personal_scope_id), "client": "a", "external_id": "a"},
        ).status_code
        == 403
    )
    assert writer.post(f"/v1/sessions/{session}/heartbeat").status_code == 403
    own_session = _session(writer, workspace.personal_scope_id)
    req = {
        "session_id": own_session,
        "event_id": str(uuid4()),
        "sequence": 1,
        "expected_version": 1,
        "generation": 1,
    }
    assert writer.post(f"/v1/tasks/{task['task_id']}/recover", json=req).status_code == 403
    assert (
        owner_client.post(
            "/v1/tasks",
            json={
                "scope_id": str(workspace.personal_scope_id),
                "session_id": session,
                "event_id": str(uuid4()),
                "sequence": 2,
                "title": "Unsafe",
                "goal": "Goal",
                "transcript": "not accepted",
            },
        ).status_code
        == 422
    )


def test_http_checkpoint_replay_heartbeat_and_delete(
    owner_client: TestClient, workspace: WorkspaceInfo
) -> None:
    session = _session(owner_client, workspace.personal_scope_id)
    task = _task(owner_client, workspace.personal_scope_id, session)
    req = {
        "session_id": session,
        "event_id": str(uuid4()),
        "sequence": 2,
        "expected_version": 1,
        "generation": 1,
        "status": "running",
        "summary": "Codex finished API; Claude next reviews tests",
        "next_step": "Review tests",
    }
    path = f"/v1/tasks/{task['task_id']}/checkpoint"
    response = owner_client.patch(path, json=req)
    assert response.status_code == 200, response.text
    assert owner_client.patch(path, json=req).json() == response.json()
    changed = {**req, "summary": "Different"}
    assert owner_client.patch(path, json=changed).status_code == 409
    assert owner_client.post(f"/v1/sessions/{session}/heartbeat").status_code == 200
    detail = owner_client.get(f"/v1/tasks/{task['task_id']}").json()
    assert detail["version"] == 2
    assert detail["summary"] == req["summary"]
    assert len(owner_client.get(f"/v1/tasks/{task['task_id']}/events").json()["items"]) == 2
    deleted = owner_client.request(
        "DELETE",
        f"/v1/tasks/{task['task_id']}",
        json={
            "session_id": session,
            "event_id": str(uuid4()),
            "sequence": 3,
            "expected_version": 2,
            "generation": 1,
        },
    )
    assert deleted.status_code == 200
    assert owner_client.get(f"/v1/tasks/{task['task_id']}").status_code == 404
    assert owner_client.get(
        "/v1/tasks", params={"scope_id": str(workspace.personal_scope_id)}
    ).json() == {"items": []}


def test_racing_checkpoint_one_effect(
    owner_client: TestClient,
    workspace: WorkspaceInfo,
    client_for_token: Callable[[str], TestClient],
    engine: Engine,
) -> None:
    session = _session(owner_client, workspace.personal_scope_id)
    task = _task(owner_client, workspace.personal_scope_id, session)
    event = str(uuid4())
    req = {
        "session_id": session,
        "event_id": event,
        "sequence": 2,
        "expected_version": 1,
        "generation": 1,
        "summary": "Once",
    }

    def send() -> tuple[int, dict[str, object]]:
        with client_for_token(workspace.owner_token) as client:
            response = client.patch(f"/v1/tasks/{task['task_id']}/checkpoint", json=req)
            return response.status_code, dict(response.json())

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: send(), range(2)))
    assert results[0] == results[1]
    assert results[0][0] == 200
    with engine.begin() as conn:
        assert (
            conn.execute(
                select(tasks.c.version).where(tasks.c.id == UUID(str(task["task_id"])))
            ).scalar_one()
            == 2
        )
        assert (
            len(conn.execute(select(task_events.c.id).where(task_events.c.id == UUID(event))).all())
            == 1
        )


def test_duplicate_task_id_and_cross_session_event_id_are_conflicts(
    owner_client: TestClient, workspace: WorkspaceInfo
) -> None:
    first = _session(owner_client, workspace.personal_scope_id, "first")
    second = _session(owner_client, workspace.personal_scope_id, "second")
    event = str(uuid4())
    task_id = str(uuid4())
    req = {
        "scope_id": str(workspace.personal_scope_id),
        "session_id": first,
        "event_id": event,
        "sequence": 1,
        "task_id": task_id,
        "title": "One",
        "goal": "Goal",
    }
    assert owner_client.post("/v1/tasks", json=req).status_code == 201
    assert owner_client.post("/v1/tasks", json={**req, "session_id": second}).status_code == 409
    assert (
        owner_client.post(
            "/v1/tasks", json={**req, "session_id": second, "event_id": str(uuid4())}
        ).status_code
        == 409
    )

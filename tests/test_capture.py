"""Explicit selected capture, replay fences and credential isolation."""

from uuid import uuid4


def session(client, workspace):
    result = client.post(
        "/v1/sessions",
        json={
            "scope_id": str(workspace.personal_scope_id),
            "client": "codex",
            "external_id": str(uuid4()),
        },
    )
    assert result.status_code == 201, result.text
    return result.json()["id"]


def selected(**overrides):
    return {
        "event_id": str(uuid4()),
        "sequence": 1,
        "kind": "message",
        "author": "user",
        "content": "Selected requirement only",
        **overrides,
    }


def test_event_redaction_replay_and_sequence(owner_client, workspace):
    sid = session(owner_client, workspace)
    event = selected(content="token=privateValue Bearer abc.def mem_abcdefghijk")
    url = f"/v1/sessions/{sid}/events"
    assert owner_client.post(url, json=event).status_code == 201
    assert owner_client.post(url, json=event).json()["deduplicated"]
    assert owner_client.post(url, json={**event, "content": "Different"}).status_code == 409
    assert owner_client.post(url, json=selected()).status_code == 409
    assert owner_client.post(url, json=selected(sequence=2)).status_code == 201
    rows = owner_client.get(url).json()["items"]
    assert [row["sequence"] for row in rows] == [2, 1]
    assert rows[1]["content"] == "[REDACTED] [REDACTED] [REDACTED]"


def test_capture_cannot_write_another_credential(owner_client, workspace, make_agent_client):
    agent = make_agent_client([workspace.personal_scope_id], ["task:read", "task:write"])
    sid = session(owner_client, workspace)
    url = f"/v1/sessions/{sid}/events"
    assert agent.post(url, json=selected()).status_code == 403
    assert (
        agent.patch(
            f"/v1/sessions/{sid}/state",
            json={
                "expected_version": 0,
                "summary": "Impersonation",
            },
        ).status_code
        == 403
    )
    assert agent.get(url).status_code == 200
    concealed = make_agent_client([], [])
    assert concealed.get(url).status_code == 404


def test_summary_version_and_forbidden_capture_fields(owner_client, workspace):
    sid = session(owner_client, workspace)
    url = f"/v1/sessions/{sid}/state"
    assert owner_client.get(url).json()["version"] == 0
    assert (
        owner_client.patch(
            url,
            json={
                "expected_version": 0,
                "summary": "Safe summary password=private",
            },
        ).json()["summary"]
        == "Safe summary [REDACTED]"
    )
    assert (
        owner_client.patch(
            url,
            json={
                "expected_version": 0,
                "summary": "Lost update",
            },
        ).status_code
        == 409
    )
    assert (
        owner_client.patch(
            url,
            json={
                "expected_version": 1,
                "summary": "Next selected summary",
            },
        ).json()["version"]
        == 2
    )
    events = f"/v1/sessions/{sid}/events"
    for invalid in (
        selected(hidden_reasoning="private"),
        selected(author="tool"),
        selected(content="မြန်မာ" * 2000),
        selected(actor_id=str(uuid4())),
        selected(kind="reasoning"),
        selected(content="bad\x00value"),
    ):
        assert owner_client.post(events, json=invalid).status_code == 422


def test_capture_sequence_is_separate_from_task_checkpoints(owner_client, workspace):
    sid = session(owner_client, workspace)
    result = owner_client.post(
        "/v1/tasks",
        json={
            "scope_id": str(workspace.personal_scope_id),
            "session_id": sid,
            "event_id": str(uuid4()),
            "sequence": 1,
            "title": "Task",
            "goal": "Goal",
        },
    )
    assert result.status_code == 201
    assert owner_client.post(f"/v1/sessions/{sid}/events", json=selected()).status_code == 201

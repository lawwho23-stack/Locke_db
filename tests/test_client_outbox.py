import json
from pathlib import Path

import httpx
import pytest

from memory_platform.client import APIClient, ClientError
from memory_platform.outbox import Outbox


def test_client_preserves_safe_error_and_never_redirects_token():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            409, json={"error": {"code": "version_conflict", "message": "Conflict."}}
        )

    with (
        APIClient(
            "http://localhost:8000", "mem_example", transport=httpx.MockTransport(handler)
        ) as client,
        pytest.raises(ClientError) as error,
    ):
        client.request("PATCH", "/v1/tasks/id/checkpoint", {"expected_version": 1})
    assert error.value.status == 409
    assert error.value.code == "version_conflict"
    assert seen[0].headers["authorization"] == "Bearer mem_example"
    assert "mem_example" not in str(error.value)


def test_outbox_restart_lost_response_has_stable_event_and_no_credentials(tmp_path: Path):
    path = tmp_path / "queue.sqlite"
    payload = {"event_id": "same-event", "sequence": 1}
    with Outbox(path) as queue:
        queue.enqueue("PATCH", "/v1/tasks/id/checkpoint", payload)
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            raise httpx.ReadTimeout("lost response")
        return httpx.Response(200, json={"event_id": "same-event", "version": 2})

    with (
        APIClient(
            "http://localhost:8000", "mem_secret", transport=httpx.MockTransport(handler)
        ) as client,
        Outbox(path) as queue,
    ):
        assert queue.flush(client, now=100)["queued"] == 1
        assert queue.flush(client, now=200)["synced"] == 1
        assert queue.status()[0]["state"] == "synced"
    assert calls == [payload, payload]
    assert b"mem_secret" not in path.read_bytes()
    assert path.stat().st_mode & 0o777 == 0o600


def test_outbox_conflict_blocks_following_writes_and_forbids_skill_upload(tmp_path: Path):
    with Outbox(tmp_path / "queue.sqlite") as queue:
        with pytest.raises(ValueError):
            queue.enqueue("POST", "/v1/skills", {"files": []})
        queue.enqueue("PATCH", "/v1/tasks/a/checkpoint", {"event_id": "a"})
        queue.enqueue("PATCH", "/v1/tasks/a/checkpoint", {"event_id": "b"})
        with APIClient(
            "http://localhost",
            "mem_token",
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    409, json={"error": {"code": "version_conflict", "message": "Conflict."}}
                )
            ),
        ) as client:
            result = queue.flush(client, now=100)
        assert result == {"synced": 0, "queued": 1, "blocked": 1}
        assert [row["state"] for row in queue.status()] == ["blocked", "queued"]


def test_outbox_operator_recovery_preserves_immutable_events(tmp_path: Path):
    with Outbox(tmp_path / "queue.sqlite") as queue:
        queue.enqueue("PATCH", "/v1/tasks/a/checkpoint", {"event_id": "a"})
        queue.enqueue("PATCH", "/v1/tasks/a/checkpoint", {"event_id": "b"})
        with APIClient(
            "http://localhost",
            "mem_token",
            transport=httpx.MockTransport(
                lambda r: httpx.Response(401, json={"error": {"code": "unauthenticated"}})
            ),
        ) as client:
            queue.flush(client, now=100)
        queue.retry("a")
        assert queue.status()[0]["state"] == "queued"
        with pytest.raises(ValueError):
            queue.discard("b")
        with APIClient(
            "http://localhost",
            "mem_token",
            transport=httpx.MockTransport(
                lambda r: httpx.Response(409, json={"error": {"code": "version_conflict"}})
            ),
        ) as client:
            queue.flush(client, now=101)
        queue.discard("a")
        assert queue.status()[0]["state"] == "discarded"
        seen = []
        with APIClient(
            "http://localhost",
            "mem_token",
            transport=httpx.MockTransport(
                lambda r: (seen.append(json.loads(r.content)), httpx.Response(200, json={}))[1]
            ),
        ) as client:
            assert queue.flush(client, now=102)["synced"] == 1
        assert seen == [{"event_id": "b"}]
        with pytest.raises(ValueError):
            queue.enqueue("PATCH", "/v1/tasks/a/checkpoint", {"event_id": "a", "sequence": 99})

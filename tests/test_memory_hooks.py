"""Lifecycle hooks must isolate projects, avoid loops, and replay immutable saves."""

import json
from uuid import uuid4

import httpx
import pytest

from memory_platform import hooks
from memory_platform.client import APIClient


def config_file(tmp_path, scope=None):
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    config = tmp_path / "hooks.json"
    config.write_text(
        json.dumps(
            {
                "projects": [
                    {
                        "root": str(project),
                        "scope_id": scope or str(uuid4()),
                        "api_url": "http://127.0.0.1:8000",
                        "query": "project architecture decisions",
                        "clients": {
                            "codex": {"token_env": "TEST_HOOK_TOKEN"},
                            "claude": {"token_env": "TEST_HOOK_TOKEN"},
                        },
                    }
                ]
            }
        )
    )
    config.chmod(0o600)
    return config, project


def test_unmapped_project_does_not_read_api(tmp_path):
    config, _ = config_file(tmp_path)
    result = hooks.handle_event(
        {"hook_event_name": "SessionStart", "session_id": "s", "cwd": str(tmp_path / "elsewhere")},
        client_name="codex",
        config_path=config,
        state_path=tmp_path / "state.sqlite",
    )
    assert result == {}


def test_stop_requests_only_one_save_and_skip_satisfies_it(tmp_path, monkeypatch):
    config, project = config_file(tmp_path)
    monkeypatch.setenv("TEST_HOOK_TOKEN", "test-only")
    state = tmp_path / "state.sqlite"
    event = {"session_id": "s", "cwd": str(project), "turn_id": "t1"}
    hooks.handle_event(
        event | {"hook_event_name": "UserPromptSubmit"},
        client_name="codex",
        config_path=config,
        state_path=state,
    )
    result = hooks.handle_event(
        event | {"hook_event_name": "Stop"},
        client_name="codex",
        config_path=config,
        state_path=state,
    )
    assert result["decision"] == "block"
    assert "publish" in result["reason"]
    assert (
        hooks.handle_event(
            event | {"hook_event_name": "Stop", "stop_hook_active": True},
            client_name="codex",
            config_path=config,
            state_path=state,
        )
        == {}
    )
    receipt = hooks.publish(
        {"skip_reason": "Only clarified requirements; no progress to save."},
        cwd=project,
        external_id="s",
        client_name="codex",
        config_path=config,
        state_path=state,
    )
    assert receipt["status"] == "skipped"
    assert (
        hooks.handle_event(
            event | {"hook_event_name": "Stop"},
            client_name="codex",
            config_path=config,
            state_path=state,
        )
        == {}
    )
    # A previous turn's receipt must not suppress the next turn's save.
    result = hooks.handle_event(
        event | {"hook_event_name": "Stop", "turn_id": "t2"},
        client_name="codex",
        config_path=config,
        state_path=state,
    )
    assert result["decision"] == "block"


def test_startup_retrieves_both_context_types_and_bounds_output(tmp_path, monkeypatch):
    scope = str(uuid4())
    config, project = config_file(tmp_path, scope)
    monkeypatch.setenv("TEST_HOOK_TOKEN", "test-only")

    def api(req):
        if req.url.path == "/v1/sessions":
            return httpx.Response(201, json={"id": str(uuid4()), "last_sequence": 0})
        if req.url.path.endswith("/heartbeat"):
            return httpx.Response(200, json={})
        if req.url.path == "/v1/tasks":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": str(uuid4()),
                            "scope_id": scope,
                            "title": "Unfinished task",
                            "status": "running",
                            "summary": "progress-marker",
                            "version": 1,
                            "generation": 1,
                            "owner_session_id": "other",
                            "next_step": "continue",
                        }
                    ]
                },
            )
        if req.url.path == "/v1/recall":
            body = json.loads(req.content)
            assert body["scope_ids"] == [scope]
            assert body["semantic"] is False
            return httpx.Response(200, json={"context": "fact-marker " + "x" * 18000})
        raise AssertionError(req.url)

    with APIClient(
        "http://127.0.0.1:8000", "test-only", transport=httpx.MockTransport(api)
    ) as client:
        result = hooks.handle_event(
            {"hook_event_name": "SessionStart", "session_id": "s", "cwd": str(project)},
            client_name="claude",
            config_path=config,
            state_path=tmp_path / "state.sqlite",
            api_client=client,
        )
    context = result["hookSpecificOutput"]["additionalContext"]
    assert "progress-marker" in context and "fact-marker" in context
    assert len(context) <= 16000
    assert "publish" in context


def test_invalid_summary_never_enters_journal(tmp_path, monkeypatch):
    config, project = config_file(tmp_path)
    monkeypatch.setenv("TEST_HOOK_TOKEN", "test-only")
    with pytest.raises(ValueError):
        hooks.publish(
            {
                "title": "test",
                "goal": "test",
                "status": "completed",
                "summary": "x",
                "transcript": "must not store",
            },
            cwd=project,
            external_id="s",
            client_name="codex",
            config_path=config,
            state_path=tmp_path / "state.sqlite",
        )


@pytest.fixture
def hook_api(workspace, make_scope, make_agent_client):
    scope = make_scope("project", "hook-test")
    caller = make_agent_client([scope], ["task:read", "task:write", "memory:read", "memory:write"])

    def transport(req):
        reply = caller.request(
            req.method, str(req.url), content=req.content, headers=dict(req.headers)
        )
        return httpx.Response(reply.status_code, content=reply.content, headers=reply.headers)

    with APIClient(
        "http://127.0.0.1:8000", caller.token, transport=httpx.MockTransport(transport)
    ) as client:
        yield str(scope), caller.token, client, transport


def test_save_completion_and_fresh_other_client_loads_progress_and_fact(
    tmp_path, monkeypatch, hook_api
):
    scope, token, client, _ = hook_api
    config, project = config_file(tmp_path, scope)
    monkeypatch.setenv("TEST_HOOK_TOKEN", token)
    state = tmp_path / "state.sqlite"
    progress = {
        "title": "Project architecture",
        "goal": "Verify hooks",
        "summary": "project verified hooks-marker",
        "status": "completed",
        "results": ["Local acceptance passed"],
        "memories": [{"type": "fact", "content": "project architecture fact-marker"}],
    }
    saved = hooks.publish(
        progress,
        cwd=project,
        external_id="codex-session",
        client_name="codex",
        config_path=config,
        state_path=state,
        api_client=client,
    )
    assert saved["status"] == "saved"
    task_id = saved["task_id"]
    replay = hooks.publish(
        progress,
        cwd=project,
        external_id="codex-session",
        client_name="codex",
        config_path=config,
        state_path=state,
        api_client=client,
    )
    assert replay["replayed"] is True
    assert len(client.tasks(scope)["items"]) == 1
    assert client.request("GET", f"/v1/tasks/{task_id}")["status"] == "completed"
    result = hooks.handle_event(
        {"hook_event_name": "SessionStart", "session_id": "claude-new", "cwd": str(project)},
        client_name="claude",
        config_path=config,
        state_path=state,
        api_client=client,
    )
    assert "hooks-marker" in result["hookSpecificOutput"]["additionalContext"]
    assert "fact-marker" in result["hookSpecificOutput"]["additionalContext"]
    assert state.stat().st_mode & 0o077 == 0


def test_lost_response_retries_same_write_without_duplicate_tasks(tmp_path, monkeypatch, hook_api):
    scope, token, client, real_transport = hook_api
    config, project = config_file(tmp_path, scope)
    monkeypatch.setenv("TEST_HOOK_TOKEN", token)
    state = tmp_path / "state.sqlite"
    lost = False

    def unreliable(req):
        nonlocal lost
        reply = real_transport(req)
        if req.url.path == "/v1/tasks" and req.method == "POST" and not lost:
            lost = True
            raise httpx.ReadError("lost response", request=req)
        return reply

    progress = {
        "title": "Resume test",
        "goal": "No duplicate tasks",
        "summary": "verified progress",
    }
    with APIClient(
        "http://127.0.0.1:8000", token, transport=httpx.MockTransport(unreliable)
    ) as flaky:
        pending = hooks.publish(
            progress,
            cwd=project,
            external_id="s",
            client_name="codex",
            config_path=config,
            state_path=state,
            api_client=flaky,
        )
    assert pending["status"] == "pending"
    saved = hooks.publish(
        progress,
        cwd=project,
        external_id="s",
        client_name="codex",
        config_path=config,
        state_path=state,
        api_client=client,
    )
    assert saved["status"] == "saved"
    tasks = client.tasks(scope)["items"]
    assert len(tasks) == 1 and tasks[0]["summary"] == "verified progress"
    assert tasks[0]["version"] == 2


def test_cross_session_update_is_blocked_without_changing_task(tmp_path, monkeypatch, hook_api):
    scope, token, client, _ = hook_api
    config, project = config_file(tmp_path, scope)
    monkeypatch.setenv("TEST_HOOK_TOKEN", token)
    state = tmp_path / "state.sqlite"
    saved = hooks.publish(
        {"title": "Owned", "goal": "Own work", "summary": "original"},
        cwd=project,
        external_id="owner",
        client_name="codex",
        config_path=config,
        state_path=state,
        api_client=client,
    )
    forbidden = hooks.publish(
        {
            "task_id": saved["task_id"],
            "expected_version": saved["version"],
            "generation": saved["generation"],
            "summary": "overwrite",
        },
        cwd=project,
        external_id="other",
        client_name="claude",
        config_path=config,
        state_path=state,
        api_client=client,
    )
    assert forbidden["status"] == "blocked"
    assert client.request("GET", f"/v1/tasks/{saved['task_id']}")["summary"] == "original"


def test_invalid_completion_and_empty_skip_are_rejected(tmp_path, monkeypatch):
    config, project = config_file(tmp_path)
    for payload in [
        {"skip_reason": " "},
        {"skip_reason": "skip", "summary": "work"},
        {"title": "x", "goal": "y", "status": "completed", "summary": "unverified"},
    ]:
        with pytest.raises(ValueError):
            hooks.publish(
                payload,
                cwd=project,
                external_id="s",
                client_name="codex",
                config_path=config,
                state_path=tmp_path / "state.sqlite",
            )


def test_installer_preserves_existing_hooks_and_is_idempotent(tmp_path):
    from memory_platform.hook_install import install

    settings = tmp_path / ".claude/settings.json"
    settings.parent.mkdir()
    settings.write_text(
        json.dumps(
            {
                "custom": "preserve",
                "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "existing-hook"}]}]},
            }
        )
    )
    first = install(home=tmp_path)
    again = install(home=tmp_path)
    assert first["changed"] and not again["changed"]
    merged = json.loads(settings.read_text())
    assert merged["custom"] == "preserve"
    assert merged["hooks"]["Stop"][0]["hooks"][0]["command"] == "existing-hook"
    assert len(merged["hooks"]["Stop"]) == 2
    assert set(json.loads((tmp_path / ".codex/hooks.json").read_text())["hooks"]) == {
        "SessionStart",
        "UserPromptSubmit",
        "Stop",
    }
    assert json.loads((tmp_path / ".config/locke/hooks.json").read_text()) == {"projects": []}
    assert (tmp_path / ".local/bin/locke-memory-hook").stat().st_mode & 0o100


def test_resume_after_lost_memory_response_returns_task_receipt(tmp_path, monkeypatch, hook_api):
    scope, token, client, real_transport = hook_api
    config, project = config_file(tmp_path, scope)
    monkeypatch.setenv("TEST_HOOK_TOKEN", token)
    state = tmp_path / "state.sqlite"
    lost = False

    def unreliable(req):
        nonlocal lost
        reply = real_transport(req)
        if req.url.path == "/v1/memories" and req.method == "POST" and not lost:
            lost = True
            raise httpx.ReadError("lost memory response", request=req)
        return reply

    progress = {
        "title": "Memory retry",
        "goal": "Preserve all receipts",
        "summary": "project progress",
        "memories": [{"type": "decision", "content": "project architecture retry-marker"}],
    }
    with APIClient(
        "http://127.0.0.1:8000", token, transport=httpx.MockTransport(unreliable)
    ) as flaky:
        pending = hooks.publish(
            progress,
            cwd=project,
            external_id="s",
            client_name="codex",
            config_path=config,
            state_path=state,
            api_client=flaky,
        )
    assert pending["status"] == "pending"
    saved = hooks.publish(
        progress,
        cwd=project,
        external_id="s",
        client_name="codex",
        config_path=config,
        state_path=state,
        api_client=client,
    )
    assert saved["task_id"] and saved["version"] == 2 and saved["generation"] == 1
    memories = client.request("GET", "/v1/memories", params={"scope_id": scope})["items"]
    assert len(memories) == 1


def test_stale_version_blocks_and_cannot_be_hidden_by_skip(tmp_path, monkeypatch, hook_api):
    scope, token, client, _ = hook_api
    config, project = config_file(tmp_path, scope)
    monkeypatch.setenv("TEST_HOOK_TOKEN", token)
    state = tmp_path / "state.sqlite"
    saved = hooks.publish(
        {"title": "Version", "goal": "Use current version", "summary": "one"},
        cwd=project,
        external_id="s",
        client_name="codex",
        config_path=config,
        state_path=state,
        api_client=client,
    )
    stale = hooks.publish(
        {"task_id": saved["task_id"], "expected_version": 1, "generation": 1, "summary": "stale"},
        cwd=project,
        external_id="s",
        client_name="codex",
        config_path=config,
        state_path=state,
        api_client=client,
    )
    assert stale["status"] == "blocked"
    skip = hooks.publish(
        {"skip_reason": "ignore conflict"},
        cwd=project,
        external_id="s",
        client_name="codex",
        config_path=config,
        state_path=state,
    )
    assert skip["status"] == "blocked"


def test_outage_and_malformed_event_fail_open_without_exposing_details(
    tmp_path, monkeypatch, capsys
):
    import io
    import sys

    config, project = config_file(tmp_path)
    monkeypatch.setenv("TEST_HOOK_TOKEN", "never-print-this-secret")
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"hook_event_name":"Stop","session_id":null}'))
    assert hooks.main(["event", "--config", str(config)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert "systemMessage" in result and "never-print" not in json.dumps(result)

    def outage(req):
        raise httpx.ConnectError("never-print-this-secret", request=req)

    with APIClient(
        "http://127.0.0.1:8000", "never-print-this-secret", transport=httpx.MockTransport(outage)
    ) as client:
        result = hooks.handle_event(
            {"hook_event_name": "SessionStart", "session_id": "s", "cwd": str(project)},
            client_name="codex",
            config_path=config,
            state_path=tmp_path / "state.sqlite",
            api_client=client,
        )
    assert "unavailable" in result["systemMessage"]
    assert "never-print" not in json.dumps(result)


def test_claude_each_prompt_starts_a_new_turn(tmp_path):
    config, project = config_file(tmp_path)
    state = tmp_path / "state.sqlite"
    event = {"session_id": "s", "cwd": str(project)}
    for _ in range(2):
        hooks.handle_event(
            event | {"hook_event_name": "UserPromptSubmit"},
            client_name="claude",
            config_path=config,
            state_path=state,
        )
        assert (
            hooks.handle_event(
                event | {"hook_event_name": "Stop"},
                client_name="claude",
                config_path=config,
                state_path=state,
            )["decision"]
            == "block"
        )
        hooks.publish(
            {"skip_reason": "Nothing changed"},
            cwd=project,
            external_id="s",
            client_name="claude",
            config_path=config,
            state_path=state,
        )
        assert (
            hooks.handle_event(
                event | {"hook_event_name": "Stop"},
                client_name="claude",
                config_path=config,
                state_path=state,
            )
            == {}
        )


def test_new_session_recovers_previous_pending_save(tmp_path, monkeypatch, hook_api):
    scope, token, client, _real_transport = hook_api
    config, project = config_file(tmp_path, scope)
    monkeypatch.setenv("TEST_HOOK_TOKEN", token)
    state = tmp_path / "state.sqlite"

    def offline(req):
        raise httpx.ConnectError("offline", request=req)

    with APIClient(
        "http://127.0.0.1:8000", token, transport=httpx.MockTransport(offline)
    ) as unavailable:
        pending = hooks.publish(
            {"title": "Offline", "goal": "Recover later", "summary": "retry-marker"},
            cwd=project,
            external_id="old",
            client_name="codex",
            config_path=config,
            state_path=state,
            api_client=unavailable,
        )
    assert pending["status"] == "pending"
    loaded = hooks.handle_event(
        {"hook_event_name": "SessionStart", "session_id": "fresh", "cwd": str(project)},
        client_name="codex",
        config_path=config,
        state_path=state,
        api_client=client,
    )
    assert "retry-marker" in loaded["hookSpecificOutput"]["additionalContext"]
    assert len(client.tasks(scope)["items"]) == 1


def test_conflict_recovery_is_explicit_and_keeps_original_payload(tmp_path, monkeypatch, hook_api):
    scope, token, client, _ = hook_api
    config, project = config_file(tmp_path, scope)
    monkeypatch.setenv("TEST_HOOK_TOKEN", token)
    state = tmp_path / "state.sqlite"
    saved = hooks.publish(
        {"title": "Owned", "goal": "Keep owner", "summary": "original"},
        cwd=project,
        external_id="owner",
        client_name="codex",
        config_path=config,
        state_path=state,
        api_client=client,
    )
    blocked = hooks.publish(
        {"task_id": saved["task_id"], "expected_version": 1, "generation": 1, "summary": "stale"},
        cwd=project,
        external_id="owner",
        client_name="codex",
        config_path=config,
        state_path=state,
        api_client=client,
    )
    assert blocked["status"] == "blocked"
    result = hooks.recover_job(
        "discard",
        blocked["job_id"],
        cwd=project,
        external_id="owner",
        client_name="codex",
        config_path=config,
        state_path=state,
    )
    assert result["status"] == "discarded"
    updated = hooks.publish(
        {
            "task_id": saved["task_id"],
            "expected_version": 2,
            "generation": 1,
            "summary": "reconciled",
        },
        cwd=project,
        external_id="owner",
        client_name="codex",
        config_path=config,
        state_path=state,
        api_client=client,
    )
    assert updated["status"] == "saved" and updated["version"] == 3


def test_replay_returns_its_own_task_not_latest_task(tmp_path, monkeypatch, hook_api):
    scope, token, client, _ = hook_api
    config, project = config_file(tmp_path, scope)
    monkeypatch.setenv("TEST_HOOK_TOKEN", token)
    args = dict(
        cwd=project,
        external_id="s",
        client_name="codex",
        config_path=config,
        state_path=tmp_path / "state.sqlite",
        api_client=client,
    )
    first_payload = {"title": "First", "goal": "First task", "summary": "one"}
    first = hooks.publish(first_payload, **args)
    second = hooks.publish({"title": "Second", "goal": "Second task", "summary": "two"}, **args)
    assert first["task_id"] != second["task_id"]
    replay = hooks.publish(first_payload, **args)
    assert replay["task_id"] == first["task_id"]


def test_skip_and_single_discard_cannot_hide_another_pending_job(tmp_path, monkeypatch, hook_api):
    scope, token, client, _ = hook_api
    config, project = config_file(tmp_path, scope)
    monkeypatch.setenv("TEST_HOOK_TOKEN", token)
    state = tmp_path / "state.sqlite"
    args = dict(
        cwd=project,
        external_id="s",
        client_name="codex",
        config_path=config,
        state_path=state,
        api_client=client,
    )
    first = hooks.publish({"title": "First", "goal": "First", "summary": "one"}, **args)
    blocked = hooks.publish(
        {"task_id": first["task_id"], "expected_version": 1, "generation": 1, "summary": "stale"},
        **args,
    )
    hooks.publish({"title": "Next", "goal": "Next", "summary": "unsaved"}, **args)
    hooks.recover_job(
        "discard",
        blocked["job_id"],
        cwd=project,
        external_id="s",
        client_name="codex",
        config_path=config,
        state_path=state,
    )
    skip = hooks.publish({"skip_reason": "hide unsaved"}, **args)
    assert skip["status"] == "pending"
    stop = hooks.handle_event(
        {"hook_event_name": "Stop", "session_id": "s", "cwd": str(project)},
        client_name="codex",
        config_path=config,
        state_path=state,
    )
    assert "pending" in stop["systemMessage"]


def test_unfinished_work_is_prioritized_over_recent_completed_tasks():
    scope = str(uuid4())

    def api(req):
        return httpx.Response(
            200,
            json={
                "items": [
                    *[
                        {
                            "id": str(uuid4()),
                            "status": "completed",
                            "title": "Done",
                            "summary": "old",
                        }
                        for _ in range(25)
                    ],
                    {
                        "id": str(uuid4()),
                        "status": "blocked",
                        "title": "Active",
                        "summary": "active-marker",
                    },
                ]
            },
        )

    with APIClient(
        "http://127.0.0.1:8000", "test-only", transport=httpx.MockTransport(api)
    ) as client:
        assert "active-marker" in hooks.context(client, scope)


def test_lifecycle_client_refuses_requests_after_total_budget():
    import time

    with (
        hooks.HookAPIClient(
            "http://127.0.0.1:8000", "test-only", deadline=time.monotonic() - 1
        ) as client,
        pytest.raises(httpx.ReadTimeout),
    ):
        client.request("GET", "/v1/tasks")


def test_stop_flushes_a_save_queued_inside_network_restricted_sandbox(
    tmp_path, monkeypatch, hook_api
):
    scope, token, client, _ = hook_api
    config, project = config_file(tmp_path, scope)
    monkeypatch.setenv("TEST_HOOK_TOKEN", token)
    state = tmp_path / "state.sqlite"

    def no_network(req):
        raise httpx.ConnectError("sandbox network denied", request=req)

    with APIClient(
        "http://127.0.0.1:8000", token, transport=httpx.MockTransport(no_network)
    ) as sandbox:
        result = hooks.publish(
            {"title": "Sandbox", "goal": "Save through host", "summary": "queued"},
            cwd=project,
            external_id="s",
            client_name="codex",
            config_path=config,
            state_path=state,
            api_client=sandbox,
        )
    assert result["status"] == "pending"
    stopped = hooks.handle_event(
        {"hook_event_name": "Stop", "session_id": "s", "cwd": str(project)},
        client_name="codex",
        config_path=config,
        state_path=state,
        api_client=client,
    )
    assert "pending" not in stopped.get("systemMessage", "")
    assert client.tasks(scope)["items"][0]["summary"] == "queued"


def test_secret_like_authored_content_is_rejected_before_local_storage(tmp_path):
    config, project = config_file(tmp_path)
    state = tmp_path / "state.sqlite"
    with pytest.raises(ValueError):
        hooks.publish(
            {
                "title": "Configuration",
                "goal": "Remember config",
                "summary": "API key sk-abcdefgh123456789 should not be stored",
            },
            cwd=project,
            external_id="s",
            client_name="codex",
            config_path=config,
            state_path=state,
        )
    assert not state.exists()


def test_deferred_save_next_prompt_exposes_receipt_for_continued_task(
    tmp_path, monkeypatch, hook_api
):
    scope, token, client, _ = hook_api
    config, project = config_file(tmp_path, scope)
    monkeypatch.setenv("TEST_HOOK_TOKEN", token)
    state = tmp_path / "state.sqlite"
    event = {"session_id": "s", "cwd": str(project), "turn_id": "one"}
    hooks.handle_event(
        event | {"hook_event_name": "UserPromptSubmit"},
        client_name="codex",
        config_path=config,
        state_path=state,
    )
    queued = hooks.publish(
        {"title": "Continue", "goal": "Multiple turns", "summary": "one"},
        cwd=project,
        external_id="s",
        client_name="codex",
        config_path=config,
        state_path=state,
        defer=True,
    )
    assert queued["status"] == "queued"
    hooks.handle_event(
        event | {"hook_event_name": "Stop"},
        client_name="codex",
        config_path=config,
        state_path=state,
        api_client=client,
    )
    task = client.tasks(scope)["items"][0]
    prompt = hooks.handle_event(
        event | {"hook_event_name": "UserPromptSubmit", "turn_id": "two"},
        client_name="codex",
        config_path=config,
        state_path=state,
    )
    assert task["id"] in prompt["hookSpecificOutput"]["additionalContext"]
    assert '"version": 2' in prompt["hookSpecificOutput"]["additionalContext"]
    updated = hooks.publish(
        {"task_id": task["id"], "expected_version": 2, "generation": 1, "summary": "two"},
        cwd=project,
        external_id="s",
        client_name="codex",
        config_path=config,
        state_path=state,
        defer=True,
    )
    assert updated["status"] == "queued"
    hooks.handle_event(
        event | {"hook_event_name": "Stop", "turn_id": "two"},
        client_name="codex",
        config_path=config,
        state_path=state,
        api_client=client,
    )
    assert len(client.tasks(scope)["items"]) == 1
    assert client.tasks(scope)["items"][0]["version"] == 3

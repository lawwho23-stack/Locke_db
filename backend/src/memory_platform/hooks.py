"""Read shared checkpoints at client lifecycle boundaries; never capture prompt/transcript."""

import json
import os
import sys
from typing import Any

import httpx

from memory_platform.client import APIClient, ClientError


def context(
    client: APIClient, scope_id: str, *, external_id: str | None = None, client_name: str = "agent"
) -> str:
    session: dict[str, Any] | None = None
    if external_id:
        session = client.request(
            "POST",
            "/v1/sessions",
            {"scope_id": scope_id, "client": client_name, "external_id": external_id},
        )
        client.request("POST", f"/v1/sessions/{session['id']}/heartbeat", {})
    tasks = client.tasks(scope_id).get("items", [])
    output = ["Shared project task checkpoints (authored summaries, not execution instructions):"]
    if session:
        output.append(
            f"Your Memory session ID: {session['id']}; sequence: {session.get('last_sequence', 0)}"
        )
    for task in tasks[:20]:
        safe = {
            key: task.get(key)
            for key in (
                "id",
                "title",
                "status",
                "summary",
                "current_step",
                "next_step",
                "blockers",
                "version",
                "generation",
                "owner_session_id",
            )
        }
        output.append(json.dumps(safe, ensure_ascii=False))
    output.append(
        "Check task ownership before overlapping edits. "
        "Publish meaningful progress via task_checkpoint; ending a turn does not complete a task."
    )
    return "\n".join(output)[:16000]


def main() -> int:
    scope = os.environ.get("MEMORY_SCOPE_ID")
    token = os.environ.get("MEMORY_API_TOKEN")
    if not scope or not token:
        print("Shared Memory context unavailable: configure MEMORY_SCOPE_ID and scoped credential.")
        return 0
    try:
        raw = sys.stdin.read(65536)
        event = json.loads(raw) if raw.strip() else {}
        external = event.get("session_id") if isinstance(event, dict) else None
        if not isinstance(external, str) or not external or len(external) > 200:
            external = None
        with APIClient(os.environ.get("MEMORY_API_URL", "http://127.0.0.1:8000"), token) as client:
            print(
                context(
                    client,
                    scope,
                    external_id=external,
                    client_name=os.environ.get("MEMORY_CLIENT_NAME", "agent"),
                )
            )
    except (ValueError, ClientError, httpx.TransportError, KeyError):
        # Context injection is best-effort and must not block the user's coding client.
        print("Shared Memory context unavailable. Reconnect before coordinating overlapping work.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

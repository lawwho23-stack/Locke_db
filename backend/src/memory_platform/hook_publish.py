"""Validate authored summaries and replay writes without altering retry identities."""

import hashlib
import json
import time
from typing import Any, Literal
from uuid import UUID, uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from memory_platform.api.schemas import Content
from memory_platform.api.task_schemas import BoundedList, ShortText, TaskStatus
from memory_platform.client import APIClient, ClientError
from memory_platform.hook_config import Project
from memory_platform.hook_state import HookState
from memory_platform.services.capture import redact


class AuthoredMemory(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["fact", "decision"]
    content: Content


class Progress(BaseModel):
    model_config = ConfigDict(extra="forbid")
    skip_reason: ShortText | None = None
    task_id: UUID | None = None
    expected_version: int | None = Field(default=None, ge=1)
    generation: int | None = Field(default=None, ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    goal: str | None = Field(default=None, min_length=1, max_length=4000)
    status: TaskStatus = "running"
    summary: str = Field(default="", max_length=4000)
    current_step: ShortText = ""
    next_step: ShortText = ""
    blockers: BoundedList = Field(default_factory=list)
    artifact_refs: BoundedList = Field(default_factory=list)
    results: BoundedList = Field(default_factory=list)
    memories: list[AuthoredMemory] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def authored(self) -> "Progress":
        serialized = json.dumps(self.model_dump(mode="json"))
        if redact(serialized) != serialized:
            raise ValueError("Remove secret-like content before publishing progress.")
        if self.skip_reason is not None:
            if not self.skip_reason.strip() or self.model_fields_set != {"skip_reason"}:
                raise ValueError("A skip must contain only a non-empty reason.")
        else:
            if not self.summary.strip():
                raise ValueError("A progress summary is required.")
            if self.task_id:
                if self.expected_version is None or self.generation is None:
                    raise ValueError("An existing task requires its version and generation.")
            elif not self.title or not self.goal or self.expected_version or self.generation:
                raise ValueError("A new task requires title and goal, without version fields.")
            if self.status == "completed" and (self.blockers or not self.results):
                raise ValueError("Completion requires results/checks and no remaining blockers.")
        return self


def namespace(project: Project, client_name: str, external_id: str) -> str:
    identity = [
        str(project.root.resolve()),
        str(project.scope_id),
        project.api_url,
        client_name,
        external_id,
        project.clients[client_name].model_dump(),
    ]
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def _prepare(
    client: APIClient, project: Project, client_name: str, external_id: str, payload: dict[str, Any]
) -> list[dict[str, Any]]:
    progress = Progress.model_validate(payload)
    scope = str(project.scope_id)
    session = client.request(
        "POST",
        "/v1/sessions",
        {
            "scope_id": scope,
            "client": client_name,
            "external_id": external_id,
        },
    )
    session_id = session["id"]
    sequence = session["last_sequence"]
    operations = []
    if progress.task_id:
        task_id = str(progress.task_id)
        task = client.request("GET", f"/v1/tasks/{task_id}")
        if (
            task["scope_id"] != scope
            or task["owner_session_id"] != session_id
            or task["version"] != progress.expected_version
            or task["generation"] != progress.generation
        ):
            raise ClientError(409, "version_conflict")
        version, generation = progress.expected_version, progress.generation
    else:
        task_id = str(uuid4())
        sequence += 1
        operations.append(
            {
                "method": "POST",
                "path": "/v1/tasks",
                "body": {
                    "task_id": task_id,
                    "scope_id": scope,
                    "session_id": session_id,
                    "event_id": str(uuid4()),
                    "sequence": sequence,
                    "title": progress.title,
                    "goal": progress.goal,
                },
            }
        )
        version, generation = 1, 1
    checkpoint = progress.model_dump(
        mode="json",
        include={
            "status",
            "summary",
            "current_step",
            "next_step",
            "blockers",
            "artifact_refs",
            "results",
        },
    )
    operations.append(
        {
            "method": "PATCH",
            "path": f"/v1/tasks/{task_id}/checkpoint",
            "body": {
                **checkpoint,
                "session_id": session_id,
                "event_id": str(uuid4()),
                "sequence": sequence + 1,
                "expected_version": version,
                "generation": generation,
            },
        }
    )
    for memory in progress.memories:
        operations.append(
            {
                "method": "POST",
                "path": "/v1/memories",
                "body": {"scope_id": scope, **memory.model_dump()},
                "idempotency_key": str(uuid4()),
            }
        )
    return operations


def flush(
    state: HookState,
    key: str,
    client: APIClient,
    project: Project,
    client_name: str,
    external_id: str,
    *,
    deadline: float | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {"status": "saved"}
    for job in state.jobs(key):
        if job["status"] == "blocked":
            return {"status": "blocked", "job_id": job["id"], "error": job["error"]}
        try:
            result = state.get("job-result:" + job["id"]) | {"status": "saved"}
            operations = json.loads(job["operations"])
            if not operations:
                operations = _prepare(
                    client, project, client_name, external_id, json.loads(job["payload"])
                )
                # Commit the immutable operation bytes BEFORE sending any content write.
                state.update_job(job["id"], operations=json.dumps(operations))
            # Revalidate the original session under today's credential before replaying.
            client.request(
                "POST", "/v1/sessions/" + operations[0]["body"]["session_id"] + "/heartbeat", {}
            )
            for index in range(job["position"], len(operations)):
                if deadline is not None and time.monotonic() >= deadline:
                    raise httpx.ReadTimeout("Hook retry budget exhausted.")
                operation = operations[index]
                reply = client.request(
                    operation["method"],
                    operation["path"],
                    operation["body"],
                    idempotency_key=operation.get("idempotency_key"),
                )
                if "task_id" in reply:
                    result.update({k: reply[k] for k in ("task_id", "version", "generation")})
                state.put("job-result:" + job["id"], result)
                state.update_job(job["id"], position=index + 1)
            state.update_job(job["id"], status="saved", error=None)
            state.put(f"receipt:{key}:{job['turn']}", result)
        except (ClientError, httpx.TransportError) as exc:
            retryable = isinstance(exc, httpx.TransportError) or exc.status in {
                408,
                429,
                500,
                502,
                503,
                504,
            }
            result = {
                "status": "pending" if retryable else "blocked",
                "job_id": job["id"],
                "error": "transport_error" if isinstance(exc, httpx.TransportError) else exc.code,
            }
            state.update_job(job["id"], status=result["status"], error=result["error"])
            state.put(f"receipt:{key}:{job['turn']}", result)
            return result
    return result

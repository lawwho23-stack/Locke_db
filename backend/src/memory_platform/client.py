"""Thin HTTP client. All authorization and domain rules remain in the API."""

from typing import Any

import httpx


class ClientError(Exception):
    def __init__(self, status: int, code: str, retry_after: int | None = None) -> None:
        self.status = status
        self.code = code
        self.retry_after = retry_after
        super().__init__(f"Memory API returned {status} ({code}).")


class APIClient:
    def __init__(
        self, base_url: str, token: str, *, transport: httpx.BaseTransport | None = None
    ) -> None:
        url = httpx.URL(base_url)
        if url.scheme != "https" and not (
            url.scheme == "http" and url.host in {"localhost", "127.0.0.1", "::1"}
        ):
            raise ValueError("Use HTTPS or a local loopback API.")
        if url.username or url.password or url.query or url.fragment:
            raise ValueError("API URL must not contain credentials, query or fragment.")
        self.http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            transport=transport,
            timeout=30,
            follow_redirects=False,
            trust_env=False,
        )

    def __enter__(self) -> "APIClient":
        return self

    def __exit__(self, *args: Any) -> None:
        self.http.close()

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        params: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if not path.startswith("/v1/") or ".." in path or "://" in path or "\\" in path:
            raise ValueError("Only Memory API paths are allowed.")
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        response = self.http.request(method, path, json=payload, params=params, headers=headers)
        if not response.is_success:
            code = "http_error"
            try:
                data = response.json()
                candidate = data.get("error", {}).get("code", "http_error")
                if isinstance(candidate, str) and candidate.replace("_", "").isalnum():
                    code = candidate[:64]
            except (ValueError, AttributeError, TypeError):
                pass
            retry = response.headers.get("retry-after", "")
            raise ClientError(response.status_code, code, int(retry) if retry.isdecimal() else None)
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("Invalid Memory API response.")
        return data

    def resolve_skill(self, scope_id: str, command: str) -> dict[str, Any]:
        return self.request(
            "GET", "/v1/skills/resolve", params={"scope_id": scope_id, "command": command}
        )

    def tasks(self, scope_id: str) -> dict[str, Any]:
        return self.request("GET", "/v1/tasks", params={"scope_id": scope_id})

    def recall(
        self, query: str, scope_ids: list[str], session_id: str | None = None, **options: Any
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"query": query, "scope_ids": scope_ids, **options}
        if session_id is not None:
            body["session_id"] = session_id
        return self.request("POST", "/v1/recall", body)

    def remember(
        self,
        scope_id: str,
        content: str,
        type: str = "fact",
        *,
        idempotency_key: str | None = None,
        **options: Any,
    ) -> dict[str, Any]:
        return self.request(
            "POST",
            "/v1/memories",
            {"scope_id": scope_id, "content": content, "type": type, **options},
            idempotency_key=idempotency_key,
        )

    def get_memory(self, memory_id: str) -> dict[str, Any]:
        return self.request("GET", f"/v1/memories/{memory_id}")

    def update(
        self, memory_id: str, changes: dict[str, Any], *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        return self.request(
            "PATCH", f"/v1/memories/{memory_id}", changes, idempotency_key=idempotency_key
        )

    def forget(self, memory_id: str, expected_version: int | None = None) -> dict[str, Any]:
        return self.request(
            "DELETE", f"/v1/memories/{memory_id}", {"expected_version": expected_version}
        )

    def ingest(
        self, scope_id: str, title: str, filename: str, content_base64: str
    ) -> dict[str, Any]:
        return self.request(
            "POST",
            "/v1/sources",
            {
                "scope_id": scope_id,
                "title": title,
                "filename": filename,
                "content_base64": content_base64,
            },
        )

    def record_event(
        self, session_id: str, event: dict[str, Any], *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        return self.request(
            "POST", f"/v1/sessions/{session_id}/events", event, idempotency_key=idempotency_key
        )

    def session_state(self, session_id: str) -> dict[str, Any]:
        return self.request("GET", f"/v1/sessions/{session_id}/state")

    def update_session_state(
        self, session_id: str, expected_version: int, summary: str
    ) -> dict[str, Any]:
        return self.request(
            "PATCH",
            f"/v1/sessions/{session_id}/state",
            {"expected_version": expected_version, "summary": summary},
        )

    def graph(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.request("GET", "/v1/graph", params=params)

    def job(self, job_id: str) -> dict[str, Any]:
        return self.request("GET", f"/v1/jobs/{job_id}")

    def usage(self) -> dict[str, Any]:
        return self.request("GET", "/v1/usage")

    def ready(self) -> dict[str, Any]:
        return self.request("GET", "/ready")

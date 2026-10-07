"""Optional Redis REST cache. Entries contain IDs/scores, never retrieved source text."""

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import Engine, or_, select

from memory_platform.api.knowledge_schemas import RecallItem, RecallRequest, RecallResponse
from memory_platform.auth.principal import Principal
from memory_platform.config import Settings
from memory_platform.knowledge_tables import (
    published_source_version,
    source_chunks,
    source_versions,
    sources,
)
from memory_platform.providers import KnowledgeProvider
from memory_platform.services.access import readable_scope_ids
from memory_platform.services.evidence import current_source_evidence
from memory_platform.services.recall import estimated_tokens, recall
from memory_platform.tables import memories, scopes


class RedisCache:
    def __init__(
        self, url: str, token: str, *, transport: httpx.BaseTransport | None = None
    ) -> None:
        if not url.startswith("https://"):
            raise ValueError("Redis REST requires TLS.")
        self.url = url
        self.token = token
        self.transport = transport
        self.available = True

    def _command(self, command: list[Any]) -> Any:
        try:
            with httpx.Client(
                transport=self.transport, timeout=1, follow_redirects=False, trust_env=False
            ) as client:
                response = client.post(
                    self.url, headers={"Authorization": f"Bearer {self.token}"}, json=command
                )
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict) or "error" in data:
                    raise ValueError
                self.available = True
                return data.get("result")
        except (httpx.HTTPError, ValueError):
            self.available = False
            return None

    def get(self, key: str) -> dict[str, Any] | None:
        result = self._command(["GET", key])
        if not isinstance(result, str) or len(result) > 131072:
            return None
        try:
            data = json.loads(result)
            return data if isinstance(data, dict) else None
        except ValueError:
            return None

    def set(self, key: str, value: dict[str, Any], ttl: int) -> None:
        self._command(["SET", key, json.dumps(value, separators=(",", ":")), "EX", ttl])


def recall_key(
    principal: Principal,
    req: RecallRequest,
    revisions: list[tuple[str, int, int]],
    model: str,
    dimensions: int,
) -> str:
    data = {
        "algorithm": "rrf60-v1-byte-budget",
        "workspace": str(principal.workspace_id),
        "actor": str(principal.actor_id),
        "credential": str(principal.credential_id),
        "grants": sorted(
            (str(s), sorted(c.value for c in caps)) for s, caps in principal.grants.items()
        ),
        "revisions": sorted(revisions),
        "model": model,
        "dimensions": dimensions,
        "request": req.model_dump(mode="json"),
    }
    digest = hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return "memory:recall:v1:" + digest


def _hydrate(
    engine: Engine, principal: Principal, req: RecallRequest, data: dict[str, Any]
) -> RecallResponse | None:
    try:
        ids = readable_scope_ids(principal, req.scope_ids)
        now = datetime.now(UTC)
        context = ""
        items: list[RecallItem] = []
        session_obj = None
        stored = data["items"]
        if not isinstance(stored, list) or len(stored) > req.limit:
            return None
        with engine.begin() as conn:
            if req.session_id is not None:
                from memory_platform.services.recall import _session_section

                session_obj, section = _session_section(conn, principal, req.session_id, ids)
                if section:
                    allowance = req.token_budget * 3
                    raw = section.encode("utf-8")[:allowance].decode("utf-8", errors="ignore")
                    if not raw:
                        return None
                    context = raw
            for item in stored:
                identifier = UUID(item["id"])
                if item["kind"] == "memory":
                    row = (
                        conn.execute(
                            select(memories).where(
                                memories.c.id == identifier,
                                memories.c.workspace_id == principal.workspace_id,
                                memories.c.scope_id.in_(ids),
                                memories.c.version == item["version"],
                                memories.c.state == "active",
                                or_(memories.c.valid_from.is_(None), memories.c.valid_from <= now),
                                or_(memories.c.valid_until.is_(None), memories.c.valid_until > now),
                                or_(
                                    memories.c.trust != "source_extracted",
                                    current_source_evidence(),
                                ),
                            )
                        )
                        .mappings()
                        .first()
                    )
                    if row is None:
                        return None
                    trust = row["trust"]
                    citation = f"memory:{identifier}@v{row['version']}"
                elif item["kind"] == "source":
                    row = (
                        conn.execute(
                            select(
                                source_chunks.c.content,
                                sources.c.scope_id,
                                sources.c.id.label("source_id"),
                                source_versions.c.version,
                                source_chunks.c.ordinal,
                                source_chunks.c.page,
                            )
                            .select_from(
                                source_chunks.join(
                                    source_versions,
                                    source_chunks.c.source_version_id == source_versions.c.id,
                                ).join(sources, source_versions.c.source_id == sources.c.id)
                            )
                            .where(
                                source_chunks.c.id == identifier,
                                sources.c.workspace_id == principal.workspace_id,
                                sources.c.scope_id.in_(ids),
                                sources.c.deleted_at.is_(None),
                                source_versions.c.version == published_source_version(),
                                source_versions.c.version == item["version"],
                                source_versions.c.status == "ready",
                            )
                        )
                        .mappings()
                        .first()
                    )
                    if row is None:
                        return None
                    trust = "source_extracted"
                    citation = f"source:{row['source_id']}@v{row['version']}#chunk={row['ordinal']}"
                    if row["page"] is not None:
                        citation += f"&page={row['page']}"
                else:
                    return None
                header = f"[{citation}; trust={trust}]\n"
                separator = "\n\n" if context else ""
                budget = req.token_budget * 3 - len((context + separator + header).encode())
                if budget <= 0:
                    return None
                raw = row["content"].encode()
                content = raw[:budget].decode(errors="ignore")
                items.append(
                    RecallItem(
                        kind=item["kind"],
                        id=identifier,
                        scope_id=row["scope_id"],
                        version=row["version"],
                        content=content,
                        citation=citation,
                        trust=trust,
                        score=float(item["score"]),
                        truncated=len(raw) > budget,
                    )
                )
                context += separator + header + content
        return RecallResponse(
            items=items,
            context=context,
            estimated_tokens=estimated_tokens(context),
            semantic_used=bool(data["semantic_used"]),
            truncated=bool(data["truncated"]),
            warnings=list(data["warnings"]),
            session=session_obj,
        )
    except (ValueError, TypeError, KeyError):
        return None


def cached_recall(
    engine: Engine,
    principal: Principal,
    req: RecallRequest,
    settings: Settings,
    provider: KnowledgeProvider | None = None,
    *,
    cache: RedisCache | None = None,
) -> RecallResponse:
    ids = readable_scope_ids(principal, req.scope_ids)
    if cache is None and settings.redis_rest_url and settings.redis_rest_token:
        cache = RedisCache(settings.redis_rest_url, settings.redis_rest_token.get_secret_value())
    if cache is None:
        return recall(engine, principal, req, provider)
    # Always read current authorization/revisions from Postgres. Database failure
    # cannot turn into a cached private-data response.
    with engine.begin() as conn:
        rows = conn.execute(
            select(scopes.c.id, scopes.c.revision, scopes.c.grant_revision).where(
                scopes.c.workspace_id == principal.workspace_id, scopes.c.id.in_(ids)
            )
        ).all()
    revisions = [(str(row.id), int(row.revision), int(row.grant_revision)) for row in rows]
    key = recall_key(
        principal,
        req,
        revisions,
        provider.model if provider else "keyword",
        provider.dimensions if provider else 0,
    )
    stored = cache.get(key)
    if stored is not None:
        response = _hydrate(engine, principal, req, stored)
        if response is not None:
            return response
    response = recall(engine, principal, req, provider)
    # Fallback responses should not mask a recovered model provider for minutes.
    if provider is not None and req.semantic and not response.semantic_used:
        return response
    cache.set(
        key,
        {
            "items": [
                {"id": str(i.id), "kind": i.kind, "version": i.version, "score": i.score}
                for i in response.items
            ],
            "semantic_used": response.semantic_used,
            "truncated": response.truncated,
            "warnings": response.warnings,
        },
        settings.cache_ttl_seconds,
    )
    return response

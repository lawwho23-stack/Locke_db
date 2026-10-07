"""Evidence rows (contract section 8).

Phase 1 only has "assertion" evidence: WHO said it and in WHICH request.
No text is copied here, so deleting a memory leaves nothing behind.
"""

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Connection, insert
from sqlalchemy.sql.selectable import Exists

from memory_platform.enums import EvidenceKind, EvidenceRole
from memory_platform.tables import memory_evidence


def add_assertion_evidence(
    conn: Connection,
    *,
    memory_id: UUID,
    actor_id: UUID,
    request_id: str,
    role: EvidenceRole = EvidenceRole.supports,
) -> UUID:
    """Record that `actor_id` asserted this memory in request `request_id`. Returns the new id."""
    evidence_id = uuid4()
    conn.execute(
        insert(memory_evidence).values(
            id=evidence_id,
            memory_id=memory_id,
            kind=EvidenceKind.assertion.value,
            role=role.value,
            actor_id=actor_id,
            request_id=request_id,
        )
    )
    return evidence_id


def current_source_evidence() -> Exists:
    """Correlated predicate: a source-derived memory needs published evidence."""
    import sqlalchemy as sa

    from memory_platform.capture_tables import source_memory_evidence
    from memory_platform.knowledge_tables import published_source_version, source_versions, sources
    from memory_platform.tables import memories

    return (
        sa.select(source_memory_evidence.c.id)
        .select_from(
            source_memory_evidence.join(
                source_versions, source_memory_evidence.c.source_version_id == source_versions.c.id
            ).join(sources, source_memory_evidence.c.source_id == sources.c.id)
        )
        .where(
            source_memory_evidence.c.memory_id == memories.c.id,
            source_memory_evidence.c.workspace_id == memories.c.workspace_id,
            source_memory_evidence.c.scope_id == memories.c.scope_id,
            sources.c.deleted_at.is_(None),
            source_versions.c.status == "ready",
            source_versions.c.version == published_source_version(),
        )
        .correlate(memories)
        .exists()
    )


def source_evidence_rows(conn: Connection, memory_id: UUID) -> list[dict[str, Any]]:
    from sqlalchemy import select

    from memory_platform.capture_tables import source_memory_evidence
    from memory_platform.knowledge_tables import published_source_version, source_versions, sources

    rows = conn.execute(
        select(source_memory_evidence)
        .select_from(
            source_memory_evidence.join(
                source_versions, source_memory_evidence.c.source_version_id == source_versions.c.id
            ).join(sources, source_memory_evidence.c.source_id == sources.c.id)
        )
        .where(
            source_memory_evidence.c.memory_id == memory_id,
            sources.c.deleted_at.is_(None),
            source_versions.c.status == "ready",
            source_versions.c.version == published_source_version(),
        )
        .order_by(source_memory_evidence.c.created_at, source_memory_evidence.c.id)
    ).mappings()
    return [{**dict(row), "kind": "source", "role": "supports"} for row in rows]


def extraction_input(chunks: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Only submitted locators may be returned by the untrusted extraction model."""
    import json

    submitted: list[dict[str, Any]] = []
    size = 0
    for chunk in chunks:
        text = str(chunk["content"])
        size += len(text.encode())
        if size > 12000:
            break
        submitted.append(chunk)
    return json.dumps(
        {
            "chunks": [
                {
                    "chunk_id": str(row["id"]),
                    "ordinal": row["ordinal"],
                    "page": row["page"],
                    "content": row["content"],
                }
                for row in submitted
            ]
        },
        ensure_ascii=False,
    ), submitted


def validate_extraction(
    value: str, submitted: list[dict[str, Any]]
) -> tuple[str, list[dict[str, Any]]]:
    """Reject the whole result before writes if any candidate invents a locator/quote."""
    import json

    from memory_platform.errors import AppError, ErrorCode

    try:
        if len(value.encode()) > 32000:
            raise ValueError
        result = json.loads(value)
        if not isinstance(result, dict) or set(result) != {"summary", "candidates"}:
            raise ValueError
        summary = result["summary"]
        candidates = result["candidates"]
        if (
            not isinstance(summary, str)
            or len(summary.encode()) > 16000
            or "\x00" in summary
            or not isinstance(candidates, list)
            or len(candidates) > 20
        ):
            raise ValueError
        available = {str(row["id"]): row for row in submitted}
        checked = []
        for candidate in candidates:
            if (
                not isinstance(candidate, dict)
                or set(candidate) != {"type", "content", "chunk_id", "quote"}
                or candidate["type"] not in {"fact", "preference", "decision", "experience"}
                or not all(
                    isinstance(candidate[key], str) for key in ("content", "chunk_id", "quote")
                )
            ):
                raise ValueError
            content = candidate["content"]
            quote = candidate["quote"]
            chunk = available.get(candidate["chunk_id"])
            if (
                chunk is None
                or not content.strip()
                or len(content.encode()) > 2000
                or "\x00" in content
                or content != quote
                or quote not in str(chunk["content"])
            ):
                raise ValueError
            checked.append({**candidate, "chunk": chunk})
        return summary, checked
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise AppError(
            ErrorCode.dependency_unavailable, "Invalid source extraction response."
        ) from None


def persist_extraction(
    conn: Connection,
    *,
    workspace_id: UUID,
    scope_id: UUID,
    source_id: UUID,
    source_version_id: UUID,
    actor_id: UUID,
    request_id: str,
    candidates: list[dict[str, Any]],
    hmac_key: bytes,
    settings: object = None,
) -> None:
    """Source provenance sets trust; an owner uploader never impersonates owner assertions."""
    from sqlalchemy import select

    from memory_platform.capture_tables import source_memory_evidence
    from memory_platform.core.normalize import content_hash, normalize_text, suppression_hmac
    from memory_platform.enums import MemoryState
    from memory_platform.services.locking import lock_scope
    from memory_platform.services.quotas import enforce_quota, lock_workspace
    from memory_platform.services.versions import append_version
    from memory_platform.tables import forget_suppressions, memories

    # Lock order is workspace row first, then scope advisory lock, matching
    # remember/tasks creation paths so concurrent writers cannot deadlock.
    lock_workspace(conn, workspace_id)
    lock_scope(conn, scope_id, shared=True)
    for candidate in candidates:
        content = str(candidate["content"])
        normalized = normalize_text(content)
        if (
            conn.execute(
                select(forget_suppressions.c.id).where(
                    forget_suppressions.c.workspace_id == workspace_id,
                    forget_suppressions.c.scope_id == scope_id,
                    forget_suppressions.c.content_hmac == suppression_hmac(normalized, hmac_key),
                )
            ).first()
            is not None
        ):
            continue
        digest = content_hash(normalized)
        row = (
            conn.execute(
                select(memories)
                .where(
                    memories.c.workspace_id == workspace_id,
                    memories.c.scope_id == scope_id,
                    memories.c.type == candidate["type"],
                    memories.c.content_hash == digest,
                    memories.c.trust == "source_extracted",
                    memories.c.state.in_(["draft", "active"]),
                )
                .with_for_update()
            )
            .mappings()
            .first()
        )
        if row is None:
            enforce_quota(conn, workspace_id, settings, "memories")
            memory_id = uuid4()
            conn.execute(
                insert(memories).values(
                    id=memory_id,
                    workspace_id=workspace_id,
                    scope_id=scope_id,
                    type=candidate["type"],
                    content=content,
                    content_hash=digest,
                    trust="source_extracted",
                    state="draft",
                    created_by=actor_id,
                )
            )
            append_version(
                conn,
                memory_id=memory_id,
                version=1,
                content=content,
                state=MemoryState.draft,
                labels=[],
                fact_key=None,
                actor_id=actor_id,
                reason="source_extracted",
            )
        else:
            memory_id = row["id"]
        from typing import cast

        from sqlalchemy.dialects.postgresql import insert as pg_insert

        chunk = cast(dict[str, Any], candidate["chunk"])
        conn.execute(
            pg_insert(source_memory_evidence)
            .values(
                workspace_id=workspace_id,
                scope_id=scope_id,
                memory_id=memory_id,
                source_id=source_id,
                source_version_id=source_version_id,
                chunk_id=chunk["id"],
                ordinal=chunk["ordinal"],
                page=chunk["page"],
                actor_id=actor_id,
                request_id=request_id,
            )
            .on_conflict_do_nothing(constraint="uq_source_memory_evidence_memory_chunk")
        )

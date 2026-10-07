"""Bounded hybrid retrieval with exact vectors, RRF and current-version evidence."""

import math
import re
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Engine, select

from memory_platform.api.knowledge_schemas import (
    RecallItem,
    RecallRequest,
    RecallResponse,
    SessionContext,
    SessionEvent,
)
from memory_platform.auth.principal import Principal
from memory_platform.capture_tables import capture_events, session_summaries
from memory_platform.core.normalize import normalize_text
from memory_platform.knowledge_tables import (
    memory_embeddings,
    published_source_version,
    source_chunks,
    source_versions,
    sources,
)
from memory_platform.providers import KnowledgeProvider, validate_vectors
from memory_platform.services.access import readable_scope_ids
from memory_platform.services.evidence import current_source_evidence
from memory_platform.tables import memories


def estimated_tokens(text: str) -> int:
    return math.ceil(len(text.encode("utf-8")) / 3)


def _normalized_sql(column: Any) -> Any:
    return sa.func.regexp_replace(
        sa.func.replace(sa.func.replace(sa.func.lower(column), "\u200b", ""), "\ufeff", ""),
        r"\s+",
        " ",
        "g",
    )


def _keyword(column: Any, query: str) -> tuple[Any, Any]:
    normalized = _normalized_sql(column)
    # Literal matching complements English full text for Myanmar and mixed-language queries.
    terms = list(dict.fromkeys(re.findall(r"\S+", normalize_text(query))))[:20]
    escaped = [term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") for term in terms]
    literal = sum(
        (
            sa.case((normalized.ilike("%" + term + "%", escape="\\"), 1.0), else_=0.0)
            for term in escaped
        ),
        sa.literal(0.0),
    )
    document = sa.func.to_tsvector("english", column)
    search = sa.func.plainto_tsquery("english", query)
    score = literal + sa.func.ts_rank_cd(document, search)
    return score, score > 0


def _session_section(
    conn: Any,
    principal: Principal,
    session_id: Any,
    scope_ids: list[Any],
) -> tuple[SessionContext | None, str]:
    """Load authorized session summary + recent selected events as a context section.

    The session scope must be among the caller's readable scopes; otherwise the
    session is concealed with a 404 like any other out-of-grant record.
    """
    from uuid import UUID as _UUID

    from memory_platform.errors import AppError, ErrorCode
    from memory_platform.services.tasks import _session

    sid = session_id if isinstance(session_id, _UUID) else _UUID(str(session_id))
    session = _session(conn, principal, sid)
    if session["scope_id"] not in set(scope_ids):
        raise AppError(ErrorCode.not_found, "Not found.")
    summary_row = (
        conn.execute(select(session_summaries).where(session_summaries.c.session_id == sid))
        .mappings()
        .first()
    )
    summary = str(summary_row["summary"]) if summary_row else ""
    version = int(summary_row["version"]) if summary_row else 0
    event_rows = (
        conn.execute(
            select(
                capture_events.c.sequence,
                capture_events.c.kind,
                capture_events.c.author,
                capture_events.c.content,
                capture_events.c.tool_name,
            )
            .where(capture_events.c.session_id == sid)
            .order_by(capture_events.c.sequence.desc())
            .limit(5)
        )
        .mappings()
        .all()
    )
    events = [
        SessionEvent(
            sequence=int(row["sequence"]),
            kind=str(row["kind"]),
            author=str(row["author"]),
            content=str(row["content"])[:2000],
            tool_name=row["tool_name"],
        )
        for row in reversed(event_rows)
    ]
    lines = [f"[session:{sid}; scope={session['scope_id']}]"]
    if summary:
        lines.append(f"Summary: {summary[:4000]}")
    for event in events:
        label = event.tool_name or event.kind
        lines.append(f"#{event.sequence} {event.author}/{label}: {event.content[:2000]}")
    section = "\n".join(lines) if (summary or events) else ""
    context_obj = SessionContext(
        session_id=sid,
        scope_id=session["scope_id"],
        summary=summary,
        summary_version=version,
        recent_events=events,
    )
    return context_obj, section


def recall(
    engine: Engine,
    principal: Principal,
    req: RecallRequest,
    provider: KnowledgeProvider | None = None,
) -> RecallResponse:
    scope_ids = readable_scope_ids(principal, req.scope_ids)
    if not scope_ids:
        return RecallResponse(
            items=[], context="", estimated_tokens=0, semantic_used=False, truncated=False
        )
    vector: list[float] | None = None
    warnings: list[str] = []
    if req.semantic and provider is not None:
        try:
            vector = validate_vectors(
                provider.embed(principal.workspace_id, [req.query]), 1, provider.dimensions
            )[0]
        except Exception:
            # Embedding availability changes retrieval quality, never scope enforcement.
            warnings.append("Semantic retrieval unavailable; keyword results returned.")
    elif req.semantic:
        warnings.append("Semantic retrieval is not configured; keyword results returned.")
    now = datetime.now(UTC)
    memory_filter = [
        memories.c.workspace_id == principal.workspace_id,
        memories.c.scope_id.in_(scope_ids),
        memories.c.state == "active",
        sa.or_(memories.c.valid_from.is_(None), memories.c.valid_from <= now),
        sa.or_(memories.c.valid_until.is_(None), memories.c.valid_until > now),
        sa.or_(memories.c.trust != "source_extracted", current_source_evidence()),
    ]
    source_filter = [
        sources.c.workspace_id == principal.workspace_id,
        sources.c.scope_id.in_(scope_ids),
        sources.c.deleted_at.is_(None),
        source_versions.c.version == published_source_version(),
        source_versions.c.status == "ready",
    ]
    source_join = sources.join(source_versions, source_versions.c.source_id == sources.c.id).join(
        source_chunks, source_chunks.c.source_version_id == source_versions.c.id
    )
    memory_columns = [
        memories.c.id,
        memories.c.scope_id,
        memories.c.version,
        memories.c.content,
        memories.c.trust,
        memories.c.type,
    ]
    source_columns = [
        source_chunks.c.id,
        sources.c.scope_id,
        source_versions.c.version,
        source_chunks.c.content,
        sources.c.id.label("source_id"),
        sources.c.title,
        source_chunks.c.page,
        source_chunks.c.ordinal,
    ]
    candidates: dict[str, dict[str, Any]] = {}
    channels: list[list[str]] = []

    def collect(rows: Any, kind: str) -> list[str]:
        keys = []
        for row in rows:
            key = kind + ":" + str(row["id"])
            candidates[key] = {**dict(row), "kind": kind}
            keys.append(key)
        return keys

    with engine.begin() as conn:
        score, condition = _keyword(memories.c.content, req.query)
        channels.append(
            collect(
                conn.execute(
                    select(*memory_columns)
                    .where(*memory_filter, condition)
                    .order_by(score.desc(), memories.c.id)
                    .limit(30)
                ).mappings(),
                "memory",
            )
        )
        score, condition = _keyword(source_chunks.c.content, req.query)
        channels.append(
            collect(
                conn.execute(
                    select(*source_columns)
                    .select_from(source_join)
                    .where(*source_filter, condition)
                    .order_by(score.desc(), source_chunks.c.id)
                    .limit(30)
                ).mappings(),
                "source",
            )
        )
        if vector is not None and provider is not None:
            valid = sa.and_(
                memory_embeddings.c.model == provider.model,
                memory_embeddings.c.dimensions == provider.dimensions,
            )
            distance = sa.case(
                (valid, memory_embeddings.c.embedding.cosine_distance(vector)), else_=None
            )
            join = memories.join(
                memory_embeddings,
                sa.and_(
                    memory_embeddings.c.memory_id == memories.c.id,
                    memory_embeddings.c.memory_version == memories.c.version,
                    memory_embeddings.c.content_hash == memories.c.content_hash,
                ),
            )
            channels.append(
                collect(
                    conn.execute(
                        select(*memory_columns)
                        .select_from(join)
                        .where(*memory_filter, valid)
                        .order_by(distance, memories.c.id)
                        .limit(30)
                    ).mappings(),
                    "memory",
                )
            )
            valid = sa.and_(
                source_chunks.c.embedding_model == provider.model,
                source_chunks.c.embedding_dimensions == provider.dimensions,
                source_chunks.c.embedding.is_not(None),
            )
            distance = sa.case(
                (valid, source_chunks.c.embedding.cosine_distance(vector)), else_=None
            )
            channels.append(
                collect(
                    conn.execute(
                        select(*source_columns)
                        .select_from(source_join)
                        .where(*source_filter, valid)
                        .order_by(distance, source_chunks.c.id)
                        .limit(30)
                    ).mappings(),
                    "source",
                )
            )
        preferences = collect(
            conn.execute(
                select(*memory_columns)
                .where(*memory_filter, memories.c.type == "preference")
                .order_by(memories.c.importance.desc(), memories.c.id)
                .limit(2)
            ).mappings(),
            "memory",
        )
        ranks: dict[str, float] = {}
        for channel in channels:
            for rank, key in enumerate(channel, 1):
                ranks[key] = ranks.get(key, 0.0) + 1 / (60 + rank)
        order = preferences + [
            key for key in sorted(ranks, key=lambda k: (-ranks[k], k)) if key not in preferences
        ]
        items: list[RecallItem] = []
        context = ""
        truncated = len(order) > req.limit
        session_obj: SessionContext | None = None
        if req.session_id is not None:
            session_obj, section = _session_section(conn, principal, req.session_id, scope_ids)
            if section:
                allowance = req.token_budget * 3
                raw_section = section.encode("utf-8")[:allowance].decode("utf-8", errors="ignore")
                if raw_section:
                    context = raw_section
                    truncated |= len(section.encode("utf-8")) > allowance
        for key in order[: req.limit]:
            row = candidates[key]
            if row["kind"] == "memory":
                citation = f"memory:{row['id']}@v{row['version']}"
                trust = row["trust"]
            else:
                citation = f"source:{row['source_id']}@v{row['version']}#chunk={row['ordinal']}"
                if row["page"] is not None:
                    citation += f"&page={row['page']}"
                trust = "source_extracted"
            header = f"[{citation}; trust={trust}]\n"
            separator = "\n\n" if context else ""
            byte_budget = req.token_budget * 3 - len((context + separator + header).encode("utf-8"))
            if byte_budget <= 0:
                truncated = True
                break
            raw = row["content"].encode("utf-8")
            content = raw[:byte_budget].decode("utf-8", errors="ignore")
            cut = len(raw) > byte_budget
            if not content:
                truncated = True
                break
            items.append(
                RecallItem(
                    kind=row["kind"],
                    id=row["id"],
                    scope_id=row["scope_id"],
                    version=row["version"],
                    content=content,
                    citation=citation,
                    trust=trust,
                    score=ranks.get(key, 0.0),
                    truncated=cut,
                )
            )
            context += separator + header + content
            truncated |= cut
    return RecallResponse(
        items=items,
        context=context,
        estimated_tokens=estimated_tokens(context),
        semantic_used=vector is not None,
        truncated=truncated,
        warnings=warnings,
        session=session_obj,
    )

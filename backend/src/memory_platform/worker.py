"""Durable knowledge worker. External work never holds a database transaction.

Jobs are at least once; lease tokens fence stale workers. Publication locks the source
before the job, matching replacement/deletion lock order. Run `--once` for schedulers,
or continuously with a modest polling interval for local development.
"""

import argparse
import hashlib
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy import Engine, and_, delete, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from memory_platform.config import Settings, get_settings
from memory_platform.db import make_engine
from memory_platform.errors import AppError
from memory_platform.knowledge_tables import (
    knowledge_jobs,
    memory_embeddings,
    source_chunks,
    source_versions,
    sources,
)
from memory_platform.providers import (
    KnowledgeProvider,
    configured_provider,
    validate_vectors,
)
from memory_platform.services.evidence import (
    extraction_input,
    persist_extraction,
    validate_extraction,
)
from memory_platform.services.sources import chunk_pages, parse_document
from memory_platform.storage import LocalStorage
from memory_platform.tables import memories, scopes, workspaces

LEASE_SECONDS = 120
MAX_ATTEMPTS = 5


def queue_missing_memory_embeddings(
    engine: Engine, provider: KnowledgeProvider, limit: int = 100
) -> int:
    """Lazy scanner avoids changing the Phase 1 transaction/idempotency contract."""
    now = datetime.now(UTC)
    count = 0
    with engine.begin() as conn:
        exists = (
            select(memory_embeddings.c.id)
            .where(
                memory_embeddings.c.memory_id == memories.c.id,
                memory_embeddings.c.memory_version == memories.c.version,
                memory_embeddings.c.model == provider.model,
                memory_embeddings.c.dimensions == provider.dimensions,
            )
            .exists()
        )
        model_key = hashlib.sha256(
            (provider.model + ":" + str(provider.dimensions)).encode()
        ).hexdigest()[:20]
        job_key = (
            "memory:"
            + memories.c.id.cast(sa.Text())
            + ":"
            + memories.c.version.cast(sa.Text())
            + ":"
            + model_key
        )
        already_queued = (
            select(knowledge_jobs.c.id)
            .where(
                knowledge_jobs.c.workspace_id == memories.c.workspace_id,
                knowledge_jobs.c.dedup_key == job_key,
            )
            .exists()
        )
        rows = conn.execute(
            select(memories.c.id, memories.c.workspace_id, memories.c.version)
            .where(
                memories.c.state == "active",
                or_(memories.c.valid_until.is_(None), memories.c.valid_until > now),
                ~exists,
                ~already_queued,
            )
            .order_by(memories.c.updated_at, memories.c.id)
            .limit(limit)
        ).mappings()
        for row in rows:
            model_key = hashlib.sha256(
                (provider.model + ":" + str(provider.dimensions)).encode()
            ).hexdigest()[:20]
            result = conn.execute(
                pg_insert(knowledge_jobs)
                .values(
                    workspace_id=row["workspace_id"],
                    dedup_key=f"memory:{row['id']}:{row['version']}:{model_key}",
                    kind="memory_embedding",
                    target_id=row["id"],
                    target_version=row["version"],
                )
                .on_conflict_do_nothing()
            )
            count += result.rowcount
    return count


def claim_job(engine: Engine, workspace_id: UUID | None = None) -> dict[str, Any] | None:
    now = datetime.now(UTC)
    # Finish terminal leases separately from source updates to preserve delete lock order.
    with engine.begin() as conn:
        expired = (
            conn.execute(
                update(knowledge_jobs)
                .where(
                    knowledge_jobs.c.status == "leased",
                    knowledge_jobs.c.lease_until < now,
                    knowledge_jobs.c.attempts >= MAX_ATTEMPTS,
                )
                .values(
                    status="failed",
                    error_code="worker_lease_expired",
                    finished_at=now,
                    lease_token=None,
                    lease_until=None,
                )
                .returning(knowledge_jobs.c.target_id, knowledge_jobs.c.kind)
            )
            .mappings()
            .all()
        )
    expired_sources = [row["target_id"] for row in expired if row["kind"] == "source"]
    if expired_sources:
        with engine.begin() as conn:
            conn.execute(
                update(source_versions)
                .where(
                    source_versions.c.id.in_(expired_sources),
                    source_versions.c.status.in_(["queued", "processing"]),
                )
                .values(status="failed", error_code="worker_lease_expired")
            )
    with engine.begin() as conn:
        row = (
            conn.execute(
                select(knowledge_jobs)
                .where(
                    knowledge_jobs.c.attempts < MAX_ATTEMPTS,
                    *([knowledge_jobs.c.workspace_id == workspace_id] if workspace_id else []),
                    or_(
                        and_(
                            knowledge_jobs.c.status == "queued",
                            knowledge_jobs.c.available_at <= now,
                        ),
                        and_(
                            knowledge_jobs.c.status == "leased", knowledge_jobs.c.lease_until < now
                        ),
                    ),
                )
                .order_by(knowledge_jobs.c.available_at, knowledge_jobs.c.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        token = uuid4()
        conn.execute(
            update(knowledge_jobs)
            .where(knowledge_jobs.c.id == row["id"])
            .values(
                status="leased",
                attempts=row["attempts"] + 1,
                lease_token=token,
                lease_until=now + timedelta(seconds=LEASE_SECONDS),
            )
        )
        return {**dict(row), "attempts": row["attempts"] + 1, "lease_token": token}


def _leased(conn: Any, job: dict[str, Any], *, lock: bool = False) -> bool:
    query = select(knowledge_jobs.c.id).where(
        knowledge_jobs.c.id == job["id"],
        knowledge_jobs.c.status == "leased",
        knowledge_jobs.c.lease_token == job["lease_token"],
        knowledge_jobs.c.lease_until > datetime.now(UTC),
    )
    if lock:
        query = query.with_for_update()
    return conn.execute(query).first() is not None


def _renew(engine: Engine, job: dict[str, Any]) -> bool:
    with engine.begin() as conn:
        result = conn.execute(
            update(knowledge_jobs)
            .where(
                knowledge_jobs.c.id == job["id"],
                knowledge_jobs.c.status == "leased",
                knowledge_jobs.c.lease_token == job["lease_token"],
                knowledge_jobs.c.lease_until > datetime.now(UTC),
            )
            .values(lease_until=datetime.now(UTC) + timedelta(seconds=LEASE_SECONDS))
        )
        return bool(result.rowcount)


def _source_current(conn: Any, job: dict[str, Any], *, lock: bool = False) -> Any:
    query = (
        select(
            source_versions,
            sources.c.scope_id,
            sources.c.deleted_at,
            sources.c.current_version,
            sources.c.created_by,
        )
        .select_from(source_versions.join(sources, source_versions.c.source_id == sources.c.id))
        .where(
            source_versions.c.id == job["target_id"],
            source_versions.c.workspace_id == job["workspace_id"],
            sources.c.deleted_at.is_(None),
            sources.c.current_version == source_versions.c.version,
        )
    )
    if lock:
        query = query.with_for_update(of=sources)
    return conn.execute(query).mappings().first()


def _finish(conn: Any, job: dict[str, Any], status: str = "completed") -> None:
    conn.execute(
        update(knowledge_jobs)
        .where(knowledge_jobs.c.id == job["id"], knowledge_jobs.c.lease_token == job["lease_token"])
        .values(
            status=status,
            lease_token=None,
            lease_until=None,
            error_code=None,
            finished_at=datetime.now(UTC),
        )
    )


def _process_source(
    engine: Engine, job: dict[str, Any], storage: LocalStorage, provider: KnowledgeProvider | None
) -> None:
    with engine.begin() as conn:
        row = _source_current(conn, job, lock=True)
        if not _leased(conn, job, lock=True):
            return
        if row is None:
            _finish(conn, job, "cancelled")
            return
        conn.execute(
            update(source_versions)
            .where(source_versions.c.id == job["target_id"])
            .values(status="processing", error_code=None)
        )
    content = storage.get(row["storage_key"])
    if hashlib.sha256(content).hexdigest() != row["content_hash"]:
        raise ValueError("Source integrity mismatch.")
    parts = chunk_pages(parse_document(row["format"], content))
    chunks: list[dict[str, Any]] = []
    for ordinal, (page, text) in enumerate(parts):
        chunks.append(
            {
                "id": uuid4(),
                "source_version_id": row["id"],
                "workspace_id": row["workspace_id"],
                "ordinal": ordinal,
                "page": page,
                "content": text,
                "content_hash": hashlib.sha256(text.encode()).hexdigest(),
                "embedding": None,
                "embedding_model": None,
                "embedding_dimensions": None,
            }
        )
    with engine.begin() as conn:
        current = _source_current(conn, job, lock=True)
        if current is None or not _leased(conn, job, lock=True):
            return
        conn.execute(delete(source_chunks).where(source_chunks.c.source_version_id == row["id"]))
        if chunks:
            conn.execute(insert(source_chunks), chunks)
        conn.execute(
            update(source_versions)
            .where(source_versions.c.id == row["id"])
            .values(status="ready", summary=None, error_code=None)
        )
        conn.execute(
            update(scopes)
            .where(scopes.c.id == current["scope_id"])
            .values(revision=scopes.c.revision + 1)
        )
        _finish(conn, job)


def queue_missing_source_embeddings(
    engine: Engine, provider: KnowledgeProvider, limit: int = 100
) -> int:
    """Separate optional enrichment lets lexical documents remain usable during outages."""
    model_key = hashlib.sha256(
        (provider.model + ":" + str(provider.dimensions)).encode()
    ).hexdigest()[:20]
    job_key = "source-enrichment:" + source_versions.c.id.cast(sa.Text()) + ":" + model_key
    queued = (
        select(knowledge_jobs.c.id)
        .where(
            knowledge_jobs.c.workspace_id == source_versions.c.workspace_id,
            knowledge_jobs.c.dedup_key == job_key,
        )
        .exists()
    )
    missing = (
        select(source_chunks.c.id)
        .where(
            source_chunks.c.source_version_id == source_versions.c.id,
            or_(
                source_chunks.c.embedding.is_(None),
                source_chunks.c.embedding_model != provider.model,
                source_chunks.c.embedding_dimensions != provider.dimensions,
            ),
        )
        .exists()
    )
    count = 0
    with engine.begin() as conn:
        rows = (
            conn.execute(
                select(
                    source_versions.c.id, source_versions.c.workspace_id, source_versions.c.version
                )
                .select_from(
                    source_versions.join(sources, source_versions.c.source_id == sources.c.id)
                )
                .where(
                    sources.c.deleted_at.is_(None),
                    sources.c.current_version == source_versions.c.version,
                    source_versions.c.status == "ready",
                    missing,
                    ~queued,
                )
                .order_by(source_versions.c.created_at, source_versions.c.id)
                .limit(limit)
            )
            .mappings()
            .all()
        )
        for row in rows:
            result = conn.execute(
                pg_insert(knowledge_jobs)
                .values(
                    workspace_id=row["workspace_id"],
                    dedup_key=f"source-enrichment:{row['id']}:{model_key}",
                    kind="source_enrichment",
                    target_id=row["id"],
                    target_version=row["version"],
                )
                .on_conflict_do_nothing()
            )
            count += result.rowcount
    return count


def _defer_disabled(engine: Engine, job: dict[str, Any]) -> None:
    with engine.begin() as conn:
        conn.execute(
            update(knowledge_jobs)
            .where(
                knowledge_jobs.c.id == job["id"],
                knowledge_jobs.c.status == "leased",
                knowledge_jobs.c.lease_token == job["lease_token"],
            )
            .values(
                status="queued",
                attempts=job["attempts"] - 1,
                available_at=datetime.now(UTC) + timedelta(minutes=5),
                lease_token=None,
                lease_until=None,
            )
        )


def _process_source_enrichment(
    engine: Engine,
    job: dict[str, Any],
    provider: KnowledgeProvider | None,
    *,
    hmac_key: bytes | None = None,
    settings: Settings | None = None,
) -> None:
    if provider is None:
        _defer_disabled(engine, job)
        return
    with engine.begin() as conn:
        row = _source_current(conn, job)
        if row is None or row["status"] != "ready":
            if _leased(conn, job, lock=True):
                _finish(conn, job, "cancelled")
            return
        chunks = [
            dict(chunk)
            for chunk in conn.execute(
                select(source_chunks)
                .where(source_chunks.c.source_version_id == row["id"])
                .order_by(source_chunks.c.ordinal)
            ).mappings()
        ]
    for start in range(0, len(chunks), 8):
        if not _renew(engine, job):
            return
        with engine.begin() as conn:
            if _source_current(conn, job) is None:
                return
        batch = chunks[start : start + 8]
        vectors = validate_vectors(
            provider.embed(row["workspace_id"], [part["content"] for part in batch]),
            len(batch),
            provider.dimensions,
        )
        for chunk, vector in zip(batch, vectors, strict=True):
            chunk.update(
                embedding=vector,
                embedding_model=provider.model,
                embedding_dimensions=provider.dimensions,
            )
    summary: str | None = None
    candidates: list[dict[str, Any]] = []
    if hmac_key and getattr(provider, "extraction_enabled", False):
        if not _renew(engine, job):
            return
        submitted_text, submitted = extraction_input(chunks)
        summary, candidates = validate_extraction(
            provider.extract(row["workspace_id"], submitted_text), submitted
        )
    with engine.begin() as conn:
        # All quota-consuming paths acquire workspace first, then source/job locks.
        conn.execute(
            select(workspaces.c.id).where(workspaces.c.id == row["workspace_id"]).with_for_update()
        ).scalar_one()
        current = _source_current(conn, job, lock=True)
        if current is None or current["status"] != "ready" or not _leased(conn, job, lock=True):
            return
        if candidates and hmac_key:
            persist_extraction(
                conn,
                workspace_id=row["workspace_id"],
                scope_id=current["scope_id"],
                source_id=row["source_id"],
                source_version_id=row["id"],
                actor_id=current["created_by"],
                request_id="worker:" + str(job["id"]),
                candidates=candidates,
                hmac_key=hmac_key,
                settings=settings,
            )
        for chunk in chunks:
            conn.execute(
                update(source_chunks)
                .where(source_chunks.c.id == chunk["id"])
                .values(
                    embedding=chunk["embedding"],
                    embedding_model=provider.model,
                    embedding_dimensions=provider.dimensions,
                )
            )
        conn.execute(
            update(source_versions)
            .where(source_versions.c.id == row["id"])
            .values(summary=summary, error_code=None)
        )
        conn.execute(
            update(scopes)
            .where(scopes.c.id == current["scope_id"])
            .values(revision=scopes.c.revision + 1)
        )
        _finish(conn, job)


def _process_memory(
    engine: Engine, job: dict[str, Any], provider: KnowledgeProvider | None
) -> None:
    if provider is None:
        _defer_disabled(engine, job)
        return
    with engine.begin() as conn:
        row = (
            conn.execute(
                select(memories).where(
                    memories.c.id == job["target_id"],
                    memories.c.workspace_id == job["workspace_id"],
                    memories.c.version == job["target_version"],
                    memories.c.state == "active",
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            if _leased(conn, job, lock=True):
                _finish(conn, job, "cancelled")
            return
    vector = validate_vectors(
        provider.embed(row["workspace_id"], [row["content"]]), 1, provider.dimensions
    )[0]
    with engine.begin() as conn:
        current = (
            conn.execute(select(memories).where(memories.c.id == row["id"]).with_for_update())
            .mappings()
            .first()
        )
        if not _leased(conn, job, lock=True):
            return
        if (
            current is None
            or current["state"] != "active"
            or current["version"] != row["version"]
            or current["content_hash"] != row["content_hash"]
        ):
            _finish(conn, job, "cancelled")
            return
        conn.execute(
            pg_insert(memory_embeddings)
            .values(
                memory_id=row["id"],
                workspace_id=row["workspace_id"],
                memory_version=row["version"],
                content_hash=row["content_hash"],
                model=provider.model,
                dimensions=provider.dimensions,
                embedding=vector,
            )
            .on_conflict_do_nothing()
        )
        conn.execute(
            update(scopes)
            .where(scopes.c.id == current["scope_id"])
            .values(revision=scopes.c.revision + 1)
        )
        _finish(conn, job)


def _process_source_cleanup(engine: Engine, job: dict[str, Any], storage: LocalStorage) -> None:
    with engine.begin() as conn:
        deleted = conn.execute(
            select(sources.c.id).where(
                sources.c.id == job["target_id"],
                sources.c.workspace_id == job["workspace_id"],
                sources.c.deleted_at.is_not(None),
            )
        ).first()
        if not _leased(conn, job, lock=True):
            return
        if deleted is None:
            _finish(conn, job, "cancelled")
            return
        keys = (
            conn.execute(
                select(source_versions.c.storage_key).where(
                    source_versions.c.source_id == job["target_id"],
                    source_versions.c.workspace_id == job["workspace_id"],
                )
            )
            .scalars()
            .all()
        )
    # No open transaction during file IO; repeated unlink is safe after a crash.
    for key in keys:
        if not _renew(engine, job):
            return
        storage.delete(key)
    with engine.begin() as conn:
        if _leased(conn, job, lock=True):
            _finish(conn, job)


def process_job(
    engine: Engine,
    job: dict[str, Any],
    storage: LocalStorage,
    provider: KnowledgeProvider | None = None,
    *,
    hmac_key: bytes | None = None,
    settings: Settings | None = None,
) -> None:
    try:
        if job["kind"] == "source":
            _process_source(engine, job, storage, provider)
        elif job["kind"] == "source_enrichment":
            _process_source_enrichment(engine, job, provider, hmac_key=hmac_key, settings=settings)
        elif job["kind"] == "source_cleanup":
            _process_source_cleanup(engine, job, storage)
        else:
            _process_memory(engine, job, provider)
    except Exception as error:
        code = error.code.value if isinstance(error, AppError) else "processing_failed"
        with engine.begin() as conn:
            if job["kind"] in {"source", "source_enrichment"}:
                # Match delete/replace: source first, job second, version last.
                _source_current(conn, job, lock=True)
            if not _leased(conn, job, lock=True):
                return
            failed = job["attempts"] >= MAX_ATTEMPTS
            conn.execute(
                update(knowledge_jobs)
                .where(
                    knowledge_jobs.c.id == job["id"],
                    knowledge_jobs.c.lease_token == job["lease_token"],
                )
                .values(
                    status="failed" if failed else "queued",
                    available_at=datetime.now(UTC)
                    + timedelta(seconds=min(300, 2 ** job["attempts"])),
                    error_code=code,
                    lease_token=None,
                    lease_until=None,
                    finished_at=datetime.now(UTC) if failed else None,
                )
            )
            if job["kind"] == "source":
                conn.execute(
                    update(source_versions)
                    .where(
                        source_versions.c.id == job["target_id"],
                        source_versions.c.status != "cancelled",
                    )
                    .values(status="failed" if failed else "queued", error_code=code)
                )


def run_once(
    engine: Engine,
    settings: Settings,
    *,
    provider: KnowledgeProvider | None = None,
    storage: LocalStorage | None = None,
) -> bool:
    if provider is None:
        provider = configured_provider(engine, settings)
    if provider is not None:
        queue_missing_memory_embeddings(engine, provider)
        queue_missing_source_embeddings(engine, provider)
    job = claim_job(engine)
    if job is None:
        return False
    process_job(
        engine,
        job,
        storage or LocalStorage(getattr(settings, "storage_dir", Path(".data/sources"))),
        provider,
        hmac_key=settings.hmac_key_bytes,
        settings=settings,
    )
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Process durable document and embedding jobs.")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=5)
    args = parser.parse_args()
    if args.poll_seconds < 1:
        parser.error("poll interval must be at least one second")
    settings = get_settings()
    engine = make_engine(settings.database_url, prepare_threshold=settings.db_prepare_threshold)
    try:
        while True:
            worked = run_once(engine, settings)
            if args.once:
                break
            if not worked:
                time.sleep(args.poll_seconds)
    except KeyboardInterrupt:
        pass
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()

"""One durable job and bounded abandoned-upload recovery per request."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, delete, select

from memory_platform.config import Settings
from memory_platform.storage import Storage, configured_storage
from memory_platform.upload_tables import upload_receipts
from memory_platform.worker import run_once


def cleanup_expired_uploads(engine: Engine, storage: Storage, workspace_id: UUID) -> int:
    with engine.begin() as conn:
        # Allow an hour after token expiry so an in-flight upload cannot recreate a removed blob.
        row = (
            conn.execute(
                select(upload_receipts)
                .where(
                    upload_receipts.c.workspace_id == workspace_id,
                    upload_receipts.c.result.is_(None),
                    upload_receipts.c.expires_at < datetime.now(UTC) - timedelta(hours=1),
                )
                .order_by(upload_receipts.c.expires_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            .mappings()
            .first()
        )
        if row is None:
            return 0
        storage.delete(str(row["id"]))
        conn.execute(delete(upload_receipts).where(upload_receipts.c.id == row["id"]))
        return 1


def process_pending(engine: Engine, settings: Settings, workspace_id: UUID) -> dict[str, Any]:
    storage = configured_storage(settings)
    cleaned = cleanup_expired_uploads(engine, storage, workspace_id)
    bounded = settings.model_copy(update={"hosted_enrichment_batch_size": 8})
    worked = run_once(engine, bounded, storage=storage, workspace_id=workspace_id)
    return {"worked": worked, "uploads_cleaned": cleaned}

"""Schema tests: they use the real database and need NO service or API code.

`alembic check` does not look at CHECK constraints, so each rule below asserts the exact
constraint NAME that rejected the row. Each bad insert is valid in every other way, so only
the rule under test can reject it. Wave 2 code relies on these names (for example
`uq_memories_active_fact_key` becomes the `fact_key_race` error).
"""

import uuid
from typing import Any
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, insert, inspect
from sqlalchemy.exc import IntegrityError

from memory_platform.schema import metadata
from memory_platform.tables import memories, memory_relations
from tests.conftest import WorkspaceInfo, seed_workspace

EXPECTED_TABLES = {
    "workspaces",
    "scopes",
    "actors",
    "credentials",
    "credential_grants",
    "memories",
    "memory_versions",
    "memory_evidence",
    "memory_relations",
    "forget_suppressions",
    "operations",
    "activity",
}


def _constraint_name(exc_info: pytest.ExceptionInfo[IntegrityError]) -> str | None:
    """The name of the database constraint that rejected the row (from the driver error)."""
    return getattr(getattr(exc_info.value.orig, "diag", None), "constraint_name", None)


def _memory_row(ws: WorkspaceInfo, **overrides: Any) -> dict[str, Any]:
    """A valid memory row for workspace `ws`. Pass keyword overrides to break one thing."""
    row: dict[str, Any] = {
        "id": uuid.uuid4(),
        "workspace_id": ws.id,
        "scope_id": ws.personal_scope_id,
        "type": "fact",
        "content": "sample text",
        "content_hash": "hash-" + uuid.uuid4().hex,
        "trust": "owner_asserted",
        "state": "active",
        "created_by": ws.owner_actor_id,
    }
    row.update(overrides)
    return row


def _insert_memory(engine: Engine, ws: WorkspaceInfo, **overrides: Any) -> UUID:
    row = _memory_row(ws, **overrides)
    with engine.begin() as conn:
        conn.execute(insert(memories).values(**row))
    return row["id"]


def test_migration_creates_all_tables(engine: Engine) -> None:
    names = set(inspect(engine).get_table_names())
    assert names >= EXPECTED_TABLES
    # Nothing unexpected, apart from Alembic's own bookkeeping table.
    assert names == set(metadata.tables) | {"alembic_version"}


def test_memory_cannot_use_scope_from_another_workspace(
    engine: Engine, workspace: WorkspaceInfo
) -> None:
    other = seed_workspace(engine, "other-workspace")

    # Control: the same row with a scope from its OWN workspace is accepted.
    _insert_memory(engine, workspace)

    # The composite foreign key (scope_id, workspace_id) -> scopes(id, workspace_id) must
    # refuse a scope that lives in the other workspace.
    with pytest.raises(IntegrityError) as exc_info:
        _insert_memory(engine, workspace, scope_id=other.personal_scope_id)
    assert _constraint_name(exc_info) == "fk_memories_scope_id_scopes"


def test_memory_relation_cannot_point_to_itself(engine: Engine, workspace: WorkspaceInfo) -> None:
    first = _insert_memory(engine, workspace)
    second = _insert_memory(engine, workspace)

    def relation(from_id: UUID, to_id: UUID) -> dict[str, Any]:
        return {
            "workspace_id": workspace.id,
            "from_id": from_id,
            "to_id": to_id,
            "type": "related_to",
            "origin": "system",
            "created_by": workspace.owner_actor_id,
        }

    # Control: a link between two different memories is fine.
    with engine.begin() as conn:
        conn.execute(insert(memory_relations).values(**relation(first, second)))

    with pytest.raises(IntegrityError) as exc_info, engine.begin() as conn:
        conn.execute(insert(memory_relations).values(**relation(first, first)))
    assert _constraint_name(exc_info) == "ck_memory_relations_no_self_link"


def test_only_one_active_memory_per_fact_key(engine: Engine, workspace: WorkspaceInfo) -> None:
    key = "project.database"
    _insert_memory(engine, workspace, fact_key=key)

    # Allowed: the same key on a memory that is NOT active (it was replaced).
    _insert_memory(engine, workspace, fact_key=key, state="superseded")
    # Allowed: the same key on a different type.
    _insert_memory(engine, workspace, fact_key=key, type="decision")
    # Allowed: memories without a fact key can repeat freely.
    _insert_memory(engine, workspace, fact_key=None)
    _insert_memory(engine, workspace, fact_key=None)

    # Rejected: a second ACTIVE memory with the same (scope, type, fact_key).
    with pytest.raises(IntegrityError) as exc_info:
        _insert_memory(engine, workspace, fact_key=key)
    assert _constraint_name(exc_info) == "uq_memories_active_fact_key"


def test_tables_py_and_migration_have_no_drift(alembic_config: Config) -> None:
    # `command.check` raises CommandError if autogenerate would create a new migration,
    # that is, if tables.py and 0001_core.py describe different schemas.
    command.check(alembic_config)

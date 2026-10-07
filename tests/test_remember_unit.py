"""Direct service acceptance before the HTTP layer is available."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import Engine, select

from memory_platform.api.schemas import RememberRequest, UpdateRequest
from memory_platform.auth.principal import Principal
from memory_platform.enums import ActorKind, Capability, MemoryState, MemoryType
from memory_platform.errors import AppError, ErrorCode
from memory_platform.services.forget import forget_memory
from memory_platform.services.memory_queries import get_memory, list_memories
from memory_platform.services.remember import remember
from memory_platform.services.update import update_memory
from memory_platform.tables import forget_suppressions, memories, memory_versions
from tests.conftest import TEST_HMAC_KEY_HEX, WorkspaceInfo, insert_actor, insert_credential

KEY = bytes.fromhex(TEST_HMAC_KEY_HEX)


def principal(engine: Engine, ws: WorkspaceInfo, *, agent: bool = False) -> Principal:
    actor_id, credential_id = ws.owner_actor_id, ws.owner_credential_id
    if agent:
        with engine.begin() as conn:
            actor_id = insert_actor(conn, ws.id, ActorKind.agent, "service-agent")
            credential_id, _ = insert_credential(conn, ws.id, actor_id, is_admin=False)
    return Principal(
        actor_id=actor_id,
        actor_kind=ActorKind.agent if agent else ActorKind.owner,
        workspace_id=ws.id,
        credential_id=credential_id,
        is_admin=not agent,
        grants={ws.personal_scope_id: frozenset(Capability)},
    )


def test_lifecycle_duplicate_versions_and_forget(engine: Engine, workspace: WorkspaceInfo) -> None:
    owner = principal(engine, workspace)
    req = RememberRequest(scope_id=workspace.personal_scope_id, type="fact", content="Old SECRET")
    with engine.begin() as conn:
        result = remember(conn, owner, req, request_id="create", hmac_key=KEY)
        mid = UUID(result.body["id"])
        duplicate = remember(conn, owner, req, request_id="duplicate", hmac_key=KEY)
        assert duplicate.status_code == 200 and duplicate.body["deduplicated"]
        assert duplicate.scope_id is None
        updated = update_memory(
            conn,
            owner,
            mid,
            UpdateRequest(expected_version=1, content="New SECRET"),
            request_id="edit",
            hmac_key=KEY,
        )
        assert updated.body["version"] == 2
        detail = get_memory(conn, owner, mid)
        assert [v.content for v in detail.versions] == ["Old SECRET", "New SECRET"]
        assert len(detail.evidence) == 2
        forgotten = forget_memory(conn, owner, mid, request_id="forget", hmac_key=KEY)
        assert forgotten.body["version"] == 3
        row = conn.execute(select(memories).where(memories.c.id == mid)).mappings().one()
        assert row["content"] is None and row["content_hash"] is None
        assert row["labels"] == [] and row["fact_key"] is None
        versions = (
            conn.execute(select(memory_versions).where(memory_versions.c.memory_id == mid))
            .mappings()
            .all()
        )
        assert all(v["content"] is None and v["labels"] == [] for v in versions)
        assert conn.execute(
            select(forget_suppressions).where(
                forget_suppressions.c.scope_id == workspace.personal_scope_id
            )
        ).first()
        with pytest.raises(AppError) as err:
            get_memory(conn, owner, mid)
        assert err.value.code == ErrorCode.not_found


def test_owner_supersedes_and_agent_conflict_requires_promotion(
    engine: Engine, workspace: WorkspaceInfo
) -> None:
    owner, agent = principal(engine, workspace), principal(engine, workspace, agent=True)
    with engine.begin() as conn:
        first = remember(
            conn,
            owner,
            RememberRequest(
                scope_id=workspace.personal_scope_id,
                type="preference",
                content="A",
                fact_key="pref",
            ),
            request_id="a",
            hmac_key=KEY,
        )
        draft = remember(
            conn,
            agent,
            RememberRequest(
                scope_id=workspace.personal_scope_id,
                type="preference",
                content="B",
                fact_key="pref",
            ),
            request_id="b",
            hmac_key=KEY,
        )
        assert draft.body["state"] == "draft" and draft.body["conflict_with_id"] == first.body["id"]
        with pytest.raises(AppError) as err:
            update_memory(
                conn,
                agent,
                UUID(draft.body["id"]),
                UpdateRequest(expected_version=1, state="active"),
                request_id="denied",
                hmac_key=KEY,
            )
        assert err.value.code == ErrorCode.forbidden
        promoted = update_memory(
            conn,
            owner,
            UUID(draft.body["id"]),
            UpdateRequest(expected_version=1, state="active"),
            request_id="promote",
            hmac_key=KEY,
        )
        assert promoted.body["superseded_id"] == first.body["id"]
        assert promoted.body["trust"] == "owner_asserted"
        assert get_memory(conn, owner, UUID(first.body["id"])).state == MemoryState.superseded
        assert get_memory(conn, owner, UUID(draft.body["id"])).versions[-1].reason == "promoted"


def test_forget_blocks_all_old_contents_but_owner_can_reassert(
    engine: Engine, workspace: WorkspaceInfo
) -> None:
    owner, agent = principal(engine, workspace), principal(engine, workspace, agent=True)
    with engine.begin() as conn:
        req = RememberRequest(
            scope_id=workspace.personal_scope_id, type="fact", content="first", fact_key="old-key"
        )
        mid = UUID(remember(conn, owner, req, request_id="a", hmac_key=KEY).body["id"])
        update_memory(
            conn,
            owner,
            mid,
            UpdateRequest(expected_version=1, content="second", fact_key="new-key"),
            request_id="b",
            hmac_key=KEY,
        )
        forget_memory(conn, owner, mid, request_id="c", hmac_key=KEY)
        for content, fact_key in [
            ("FIRST", None),
            ("second", None),
            ("other", "old-key"),
            ("other", "new-key"),
        ]:
            with pytest.raises(AppError) as err:
                remember(
                    conn,
                    agent,
                    RememberRequest(
                        scope_id=workspace.personal_scope_id,
                        type="fact",
                        content=content,
                        fact_key=fact_key,
                    ),
                    request_id="blocked",
                    hmac_key=KEY,
                )
            assert err.value.code == ErrorCode.memory_suppressed
        assert remember(conn, owner, req, request_id="reassert", hmac_key=KEY).status_code == 201


def test_query_pagination_expiry_and_version_conflict(
    engine: Engine, workspace: WorkspaceInfo
) -> None:
    owner = principal(engine, workspace)
    with engine.begin() as conn:
        for i in range(3):
            result = remember(
                conn,
                owner,
                RememberRequest(
                    scope_id=workspace.personal_scope_id,
                    type="fact",
                    content=f"item {i}",
                    labels=["label"],
                ),
                request_id=str(i),
                hmac_key=KEY,
            )
        mid = UUID(result.body["id"])
        expired = remember(
            conn,
            owner,
            RememberRequest(
                scope_id=workspace.personal_scope_id,
                type="fact",
                content="expired",
                valid_until=datetime.now(UTC) - timedelta(seconds=1),
            ),
            request_id="exp",
            hmac_key=KEY,
        )
        kwargs = dict(
            scope_ids=None, label="label", mtype=MemoryType.fact, state=MemoryState.active, limit=2
        )
        page = list_memories(conn, owner, **kwargs, cursor=None)
        next_page = list_memories(conn, owner, **kwargs, cursor=page.next_cursor)
        assert len(page.items) == 2 and page.next_cursor
        assert len(next_page.items) == 1 and next_page.next_cursor is None
        assert len({x.id for x in page.items + next_page.items}) == 3
        assert get_memory(conn, owner, UUID(expired.body["id"])).state == MemoryState.expired
        with pytest.raises(AppError) as err:
            update_memory(
                conn,
                owner,
                mid,
                UpdateRequest(expected_version=99, labels=[]),
                request_id="stale",
                hmac_key=KEY,
            )
        assert (
            err.value.code == ErrorCode.version_conflict
            and err.value.details["current_version"] == 1
        )
        with pytest.raises(AppError) as err:
            list_memories(conn, owner, **kwargs, cursor="invalid")
        assert err.value.code == ErrorCode.bad_request

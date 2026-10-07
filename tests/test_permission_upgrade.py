from sqlalchemy import select, update

from memory_platform.admin import backfill_owner_permissions
from memory_platform.enums import Capability
from memory_platform.tables import credential_grants


def test_owner_backfill_preserves_agent_grants(engine, workspace, make_agent_client):
    agent = make_agent_client([workspace.personal_scope_id], ["memory:read"])
    legacy = ["memory:read", "memory:write", "memory:delete", "source:ingest"]
    with engine.begin() as conn:
        conn.execute(
            update(credential_grants)
            .where(credential_grants.c.credential_id == workspace.owner_credential_id)
            .values(capabilities=legacy)
        )
        assert backfill_owner_permissions(conn) == 1
        assert backfill_owner_permissions(conn) == 0
        agent_caps = conn.execute(
            select(credential_grants.c.capabilities).where(
                credential_grants.c.credential_id == agent.credential_id
            )
        ).scalar_one()
        owner_caps = conn.execute(
            select(credential_grants.c.capabilities).where(
                credential_grants.c.credential_id == workspace.owner_credential_id
            )
        ).scalar_one()
    assert agent_caps == ["memory:read"]
    assert set(owner_caps) == {cap.value for cap in Capability}

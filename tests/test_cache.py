import json
from uuid import uuid4

import httpx

from memory_platform.api.knowledge_schemas import RecallRequest
from memory_platform.auth.principal import Principal, resolve_principal
from memory_platform.cache import RedisCache, cached_recall, recall_key
from memory_platform.enums import ActorKind, Capability
from memory_platform.services.sources import upload_source
from memory_platform.storage import LocalStorage
from memory_platform.worker import claim_job, process_job
from tests.test_sources import encoded


def test_cache_key_separates_actor_grants_scope_revision_model_and_budget():
    scope = uuid4()
    p = Principal(
        uuid4(),
        ActorKind.agent,
        uuid4(),
        uuid4(),
        False,
        {scope: frozenset({Capability.memory_read})},
    )
    req = RecallRequest(query="private query", scope_ids=[scope])
    key = recall_key(p, req, [(str(scope), 1, 1)], "model-a", 3)
    assert "private query" not in key
    assert key != recall_key(p, req, [(str(scope), 2, 1)], "model-a", 3)
    assert key != recall_key(p, req, [(str(scope), 1, 2)], "model-a", 3)
    assert key != recall_key(p, req, [(str(scope), 1, 1)], "model-b", 3)
    assert key != recall_key(
        p, req.model_copy(update={"token_budget": 100}), [(str(scope), 1, 1)], "model-a", 3
    )


def test_redis_failure_bypasses_and_cache_commands_expire():
    cache = RedisCache(
        "https://example.upstash.io",
        "secret",
        transport=httpx.MockTransport(lambda r: httpx.Response(503)),
    )
    assert cache.get("key") is None
    cache.set("key", {"ids": []}, 180)
    assert cache.available is False
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"result": "OK"})

    good = RedisCache(
        "https://example.upstash.io", "secret", transport=httpx.MockTransport(handler)
    )
    good.set("key", {"ids": []}, 180)
    assert seen == [["SET", "key", '{"ids":[]}', "EX", 180]]


def test_cache_retains_ready_source_during_replacement_and_invalidates_on_publish(
    engine, workspace, settings, tmp_path
):
    entries = {}

    def redis(request):
        command = json.loads(request.content)
        if command[0] == "SET":
            entries[command[1]] = command[2]
            return httpx.Response(200, json={"result": "OK"})
        return httpx.Response(200, json={"result": entries.get(command[1])})

    cache = RedisCache("https://cache.invalid", "fake", transport=httpx.MockTransport(redis))
    with engine.begin() as conn:
        principal = resolve_principal(conn, workspace.owner_token)
    storage = LocalStorage(tmp_path)
    first = upload_source(
        engine,
        principal,
        storage,
        scope_id=workspace.personal_scope_id,
        title="Evidence",
        filename="a.txt",
        encoded=encoded("old evidence"),
    )
    process_job(engine, claim_job(engine, workspace.id), storage)
    request = RecallRequest(query="evidence", semantic=False)

    def versions():
        return [
            item.version
            for item in cached_recall(engine, principal, request, settings, cache=cache).items
        ]

    assert versions() == [1]
    upload_source(
        engine,
        principal,
        storage,
        scope_id=workspace.personal_scope_id,
        title="Evidence",
        filename="a.txt",
        encoded=encoded("new evidence"),
        source_id=first["id"],
        expected_version=1,
    )
    assert versions() == [1]
    assert versions() == [1]  # Hydrate the cached old-ready publication.
    process_job(engine, claim_job(engine, workspace.id), storage)
    assert versions() == [2]
    assert versions() == [2]
    assert all(
        "old evidence" not in value and "new evidence" not in value for value in entries.values()
    )

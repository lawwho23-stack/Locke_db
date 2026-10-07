"""Pure Phase 1 matching and policy rules, including Burmese normalization."""

import hashlib
import hmac
from datetime import UTC, datetime, timedelta

import pytest

from memory_platform.core.normalize import (
    content_hash,
    normalize_fact_key,
    normalize_text,
    suppression_hmac,
)
from memory_platform.core.policy import (
    can_transition,
    effective_state,
    initial_state,
    trust_for_actor,
)
from memory_platform.enums import ActorKind, MemoryState, MemoryType, Trust


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  Hello\n\tWORLD\u00a0 again ", "hello world again"),
        ("STRASSE Straße", "strasse strasse"),
        ("Cafe\u0301", "café"),
        ("မြန်မာ\u200bစာ\ufeff", "မြန်မာစာ"),
        ("A\u200dB\u200cC", "a\u200db\u200cc"),
        ("\u200b\ufeff \t", ""),
    ],
)
def test_normalize_text(raw: str, expected: str) -> None:
    assert normalize_text(raw) == expected
    assert normalize_text(normalize_text(raw)) == expected


def test_normalized_equivalents_hash_identically() -> None:
    assert content_hash(normalize_text(" Hello\nWORLD ")) == content_hash(
        normalize_text("hello world")
    )
    assert content_hash("hello") == hashlib.sha256(b"hello").hexdigest()
    assert (
        suppression_hmac("hello", b"test-key")
        == hmac.new(b"test-key", b"hello", hashlib.sha256).hexdigest()
    )
    assert suppression_hmac("hello", b"test-key") != suppression_hmac("hello", b"other-key")
    assert suppression_hmac("hello", b"test-key") != content_hash("hello")


def test_normalize_fact_key() -> None:
    assert normalize_fact_key("  Profile.Name:V1  ") == "profile.name:v1"


@pytest.mark.parametrize(
    ("kind", "expected"),
    [(ActorKind.owner, Trust.owner_asserted), (ActorKind.agent, Trust.agent_reported)],
)
def test_trust_from_actor(kind: ActorKind, expected: Trust) -> None:
    assert trust_for_actor(kind) == expected


@pytest.mark.parametrize("trust", list(Trust))
@pytest.mark.parametrize("mtype", list(MemoryType))
def test_initial_state_matrix(trust: Trust, mtype: MemoryType) -> None:
    expected = (
        MemoryState.active
        if trust == Trust.owner_asserted
        or (trust == Trust.agent_reported and mtype in {MemoryType.fact, MemoryType.decision})
        else MemoryState.draft
    )
    assert initial_state(trust, mtype) == expected


@pytest.mark.parametrize("old", list(MemoryState))
@pytest.mark.parametrize("new", list(MemoryState))
@pytest.mark.parametrize("actor", list(ActorKind))
def test_transition_matrix(old: MemoryState, new: MemoryState, actor: ActorKind) -> None:
    assert can_transition(old, new, actor) == (
        old == MemoryState.draft and new == MemoryState.active and actor == ActorKind.owner
    )


@pytest.mark.parametrize("state", list(MemoryState))
@pytest.mark.parametrize("offset", [None, -1, 0, 1])
def test_expiry_is_effective_without_changing_other_states(
    state: MemoryState, offset: int | None
) -> None:
    now = datetime(2026, 10, 6, tzinfo=UTC)
    until = None if offset is None else now + timedelta(seconds=offset)
    expected = (
        MemoryState.expired
        if state == MemoryState.active and until is not None and until <= now
        else state
    )
    assert effective_state(state, until, now) == expected

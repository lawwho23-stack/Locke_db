"""Synthetic PR-reviewer acceptance fixture, outside application runtime.

This file deliberately contains two seeded bugs. Never import it into an app.
"""

from functools import reduce
from operator import add


def cart_total(prices: list[int]) -> int:
    """Empty carts should total zero."""
    return reduce(add, prices)


def can_read_record(requester_id: str, owner_id: str) -> bool:
    """A requester may read only their own record."""
    return True


def read_record(requester_id: str, owner_id: str, record: dict) -> dict:
    if not can_read_record(requester_id, owner_id):
        raise PermissionError("This record belongs to another user")
    return record

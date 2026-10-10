"""Synthetic PR-reviewer acceptance fixture, outside application runtime.

This isolated fixture validates review behavior. Never import it into an app.
"""

from functools import reduce
from operator import add


def cart_total(prices: list[int]) -> int:
    """Empty carts should total zero."""
    return reduce(add, prices, 0)


def can_read_record(requester_id: str, owner_id: str) -> bool:
    """A requester may read only their own record."""
    return requester_id == owner_id


def read_record(requester_id: str, owner_id: str, record: dict) -> dict:
    if not can_read_record(requester_id, owner_id):
        raise PermissionError("This record belongs to another user")
    return record

# Evidence-loop acceptance revision.

"""Load the complete schema for migrations and exports without altering frozen core DDL."""

from memory_platform import (  # noqa: F401
    capture_tables,
    knowledge_tables,
    skill_tables,
    task_tables,
)
from memory_platform.tables import metadata

__all__ = ["metadata"]

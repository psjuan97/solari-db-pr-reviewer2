"""Data contracts shared across the review stages."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass(frozen=True)
class ReviewSpec:
    """What to review: a base schema plus the SQL files a PR changed.

    Attributes:
        title: Human label for the review (PR title, or the fixture name).
        base_schema: DDL that sets up the database the changes run against.
        changes: The changed SQL files - (filename, sql) pairs, in PR order.
        seed_data: Optional SQL run after the schema when building the known
            state - a representative data dump, so "runs cleanly" is checked
            against a realistic table, not an empty one. Every fork carries it.
    """

    title: str
    base_schema: str
    changes: List["SqlFile"]
    seed_data: Optional[str] = None


@dataclass(frozen=True)
class SqlFile:
    name: str
    sql: str


@dataclass
class ReviewOptions:
    """Knobs for one review run."""

    statement_timeout_ms: int = 10_000        # a statement slower than this fails
    sandbox_timeout_ms: int = 15 * 60 * 1000  # rolling idle window for the VM
    pg_snapshot_id: Optional[str] = None      # boot from here to skip the apt-get
    out_dir: str = "output"


@dataclass
class StatementResult:
    """The verdict for one changed SQL file."""

    name: str
    sql: str
    ok: bool
    error: str = ""   # the postgres ERROR line(s), empty when ok


@dataclass
class ReviewResult:
    title: str
    statements: List[StatementResult] = field(default_factory=list)
    sandbox_id: Optional[str] = None
    pg_snapshot_id: Optional[str] = None
    markdown: str = ""

    @property
    def all_ok(self) -> bool:
        return all(s.ok for s in self.statements)

    @property
    def failures(self) -> List[StatementResult]:
        return [s for s in self.statements if not s.ok]

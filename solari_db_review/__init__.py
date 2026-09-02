"""solari-db-pr-reviewer - review database PRs on a real, disposable postgres.

Give it a :class:`ReviewSpec` (base schema + the SQL files a PR changed). It
boots postgres in a Solari sandbox and runs each changed file on a fresh fork
of the base state, reporting which ones don't finish cleanly and the exact
Postgres error. The result is a :class:`ReviewResult` with a Markdown review.

Fixing a broken migration is a separate step - the opencode GitHub Action -
and this reviewer verifies that fix on its next run.
"""
from .config import (
    ReviewOptions,
    ReviewResult,
    ReviewSpec,
    SqlFile,
    StatementResult,
)
from .reviewer import review

__all__ = [
    "ReviewOptions",
    "ReviewResult",
    "ReviewSpec",
    "SqlFile",
    "StatementResult",
    "review",
]
__version__ = "0.1.0"

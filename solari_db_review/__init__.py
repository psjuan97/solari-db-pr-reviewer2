"""solari-db-pr-reviewer - review database PRs on a real, disposable postgres.

Give it a :class:`ReviewSpec` (base schema + the SQL files a PR changed). It
boots postgres in a Solari sandbox, runs each changed file, and for any that
doesn't finish cleanly it asks Claude for a fix and re-checks that fix in the
same sandbox. The result is a :class:`ReviewResult` with a Markdown review.
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

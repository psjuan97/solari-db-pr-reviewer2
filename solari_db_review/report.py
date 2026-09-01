"""Render the review as Markdown, and optionally post it to the PR."""
from __future__ import annotations

import subprocess
from typing import List

from .config import ReviewResult, StatementResult


def render(result: ReviewResult) -> str:
    lines: List[str] = []
    verdict = "✅ all changes run cleanly" if result.all_ok else "❌ some changes fail"
    lines.append(f"## DB PR review — {result.title}")
    lines.append("")
    lines.append(f"**{verdict}** · checked on a disposable PostgreSQL in a Solari sandbox")
    lines.append("")

    for s in result.statements:
        lines.append(_one(s))
        lines.append("")

    lines.append("---")
    lines.append("_Each file was run with `psql -v ON_ERROR_STOP=1` against the base "
                 "schema. \"Runs cleanly\" = every statement finished with no error._")
    return "\n".join(lines)


def _one(s: StatementResult) -> str:
    if s.ok:
        return f"### ✅ `{s.name}`\nRuns cleanly."

    out = [f"### ❌ `{s.name}`", "", "```", s.error.strip(), "```"]
    if s.proposed_fix:
        tag = {True: "✅ verified — runs cleanly",
               False: "⚠️ still fails",
               None: "not verified"}[s.fix_ok]
        tries = f", {s.fix_iters} attempt(s)" if s.fix_iters else ""
        out += ["", f"**Proposed fix** ({tag}{tries})"]
        if s.fix_rationale:
            out.append(f"> {s.fix_rationale}")
        out += ["", "```sql", s.proposed_fix.strip(), "```"]
    else:
        why = f" — {s.fix_rationale}" if s.fix_rationale else ""
        out += ["", f"_No fix proposed{why}._"]
    return "\n".join(out)


def post_comment(pr: str, body: str) -> None:
    """Post the review to the PR via `gh pr comment`."""
    try:
        subprocess.run(["gh", "pr", "comment", pr, "--body", body],
                       capture_output=True, text=True, check=True)
    except FileNotFoundError:
        raise SystemExit("cannot post: the 'gh' CLI is not installed")
    except subprocess.CalledProcessError as e:
        raise SystemExit(f"gh pr comment failed:\n{e.stderr.strip()}")

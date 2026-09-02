# Recreating `solari-db-pr-reviewer` from scratch

A complete, copy-paste guide to rebuild this project and its GitHub CI in a
fresh repo. Follow top to bottom. Nothing here depends on the original repo.

**What you're building:** a two-agent pipeline for database PRs.

| Agent | Runs | Does |
|---|---|---|
| **DB PR Review** | GitHub Action on `pull_request` touching `**/*.sql` | boots a real Postgres in a [Solari](https://getsolari.com) microVM, runs each changed `.sql` file on a fork of the base schema, comments a Markdown review, **fails the check** if any file errors. Detection only. |
| **opencode** | GitHub Action on a `/oc fix` PR comment | reads the review, rewrites the broken migration on the PR branch, pushes a commit. The DB PR Review then re-runs on that commit and verifies. |

---

## 0. Prerequisites

- **Python 3.12+**
- **`git`** and the **[`gh` CLI](https://cli.github.com)**, logged in: `gh auth login`
  (HTTPS, choose to authenticate git operations). The token needs the
  **`workflow`** scope to push `.github/workflows/*` — if you already logged in
  without it: `gh auth refresh -h github.com -s workflow`.
- A **Solari API key** — https://console.getsolari.com — looks like `slr_live_…`
- An **opencode.ai subscription API key** (their "Zen"/"Go" hosted gateway) —
  https://opencode.ai — looks like `sk-…`
- A GitHub account to own the new repo.

---

## 1. Create the project directory and files

```bash
mkdir my-db-pr-reviewer && cd my-db-pr-reviewer
mkdir -p solari_db_review fixtures/demo/changes .github/workflows output
touch output/.gitkeep
```

Now create each file below with exactly this content.

<details>
<summary><b><code>requirements.txt</code></b></summary>

```
solari-sandbox>=0.2.0
```
</details>

<details>
<summary><b><code>.gitignore</code></b></summary>

```gitignore
.venv/
__pycache__/
*.pyc
.env
output/*
!output/.gitkeep

# --- generic Python ---
*.py[codz]
*$py.class
*.so
.Python
build/
dist/
*.egg-info/
.eggs/
.pytest_cache/
.mypy_cache/
.ruff_cache/
htmlcov/
.coverage
```
</details>

<details>
<summary><b><code>.env.example</code></b></summary>

```bash
# Solari API key - drives the sandbox.  https://console.getsolari.com
SOLARI_API_KEY=slr_live_...

# Optional: reuse a snapshot that already has postgres installed, so runs skip
# the one-time apt-get. The first run prints the id to save here.
PG_SNAPSHOT_ID=

# The fix agent runs as a GitHub Action (opencode), not from this CLI, so no
# opencode key is needed locally. In CI it reads OPENCODE_API_KEY from repo
# secrets - see .github/workflows/opencode.yml.
```
</details>

### `solari_db_review/` — the Python package

<details>
<summary><b><code>solari_db_review/__init__.py</code></b></summary>

```python
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
```
</details>

<details>
<summary><b><code>solari_db_review/config.py</code></b></summary>

```python
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
```
</details>

<details>
<summary><b><code>solari_db_review/env.py</code></b></summary>

```python
"""Tiny .env / environment reader - no dependency on python-dotenv."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def get(name: str) -> Optional[str]:
    """Return an env var, falling back to a KEY=value line in ./.env."""
    val = os.environ.get(name, "").strip()
    if val:
        return val
    if _ENV_FILE.exists():
        for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip() or None
    return None


def load_key(name: str) -> str:
    """Like :func:`get`, but raise a helpful error when it is missing."""
    val = get(name)
    if not val:
        raise SystemExit(
            f"{name} not set.\n"
            f"  export {name}=...    or put it in ./.env  (see .env.example)"
        )
    return val
```
</details>

<details>
<summary><b><code>solari_db_review/sandbox_db.py</code></b></summary>

```python
"""A disposable PostgreSQL inside a Solari sandbox.

One microVM, one postgres. The base schema (and, later, any seed data) is
loaded **once** into a template database, ``base_state``. Every changed file
then runs on a fresh *fork* of it:

    createdb --template=base_state review     # ~0.5s, regardless of data size

Re-forked from ``base_state`` before each file, so every check starts from an
identical known state. Forking beats replaying ``schema.sql`` each time: it's
a file copy, so it stays fast once the known state is a realistic dump rather
than three ``CREATE TABLE``s. ``base_state`` is never written to.

The first ever run does a one-time ``apt-get install postgresql`` and takes a
snapshot; pass that id back (``ReviewOptions.pg_snapshot_id``) and later VMs
boot with postgres already there.

The check is simple: run a .sql file with ``psql -v ON_ERROR_STOP=1``. Exit 0
means every statement finished cleanly; non-zero means it raised, and psql
prints the ``ERROR:`` line.
"""
from __future__ import annotations

from typing import Optional, Tuple

BASE_STATE_DB = "base_state"   # the template; loaded once, never mutated
REVIEW_DB = "review"           # per-file fork

# Installs postgres (no-op if the snapshot already has it) and starts the cluster.
_BOOT = r"""
set -e
export DEBIAN_FRONTEND=noninteractive
if ! command -v psql >/dev/null; then
  apt-get update -qq
  apt-get install -y -qq postgresql postgresql-client >/dev/null 2>&1
fi
PGVER=$(ls /usr/lib/postgresql)
pg_ctlcluster "$PGVER" main start 2>/dev/null || true
sleep 2
su postgres -c "psql -tAc 'select 1'" >/dev/null
echo BOOT_OK
"""


class SandboxDb:
    """Wraps a live Solari sandbox that is running postgres."""

    def __init__(self, sandbox, statement_timeout_ms: int = 10_000) -> None:
        self._sb = sandbox
        self._timeout_ms = statement_timeout_ms
        self._base_ready = False

    async def boot(self) -> None:
        await self._sb.files.mkdir("/work")
        r = await self._sb.commands.run("bash", args=["-lc", _BOOT])
        if "BOOT_OK" not in r.stdout:
            raise RuntimeError(f"postgres boot failed:\n{r.stdout}\n{r.stderr}")

    # --- known state + forking -------------------------------------------------

    async def load_base_state(
        self, schema_sql: str, seed_sql: Optional[str] = None
    ) -> Tuple[bool, str]:
        """Build the ``base_state`` template: fresh db, load the schema, then
        optionally a seed dump. Everything forks from the result.
        """
        # The snapshot may already carry these from an earlier run - start clean.
        for db in (REVIEW_DB, BASE_STATE_DB):
            await self._dropdb(db)
        await self._createdb(BASE_STATE_DB, template="template0")
        ok, out = await self._psql_file(BASE_STATE_DB, "_schema.sql", schema_sql)
        if not ok:
            return False, out
        if seed_sql:
            ok, out = await self._psql_file(BASE_STATE_DB, "_seed.sql", seed_sql)
            if not ok:
                return False, f"(seed data failed) {out}"
        self._base_ready = True
        return True, out

    async def fork(self, db: str) -> None:
        """(Re)create ``db`` as an instant copy of ``base_state``."""
        if not self._base_ready:
            raise RuntimeError("call load_base_state() before fork()")
        await self._dropdb(db)
        await self._createdb(db, template=BASE_STATE_DB)

    # --- the per-file fork -------------------------------------------------

    async def reset(self) -> None:
        """Re-fork ``review`` from the clean base state."""
        await self.fork(REVIEW_DB)

    async def run_sql(self, name: str, sql: str) -> Tuple[bool, str]:
        """Run a SQL blob against ``review``. (ok, output)."""
        return await self._psql_file(REVIEW_DB, name, sql)

    async def snapshot(self, name: str = "pg-base") -> str:
        return await self._sb.snapshot(name)

    # --- low level ----------------------------------------------------------

    async def _dropdb(self, db: str) -> None:
        await self._sb.commands.run("bash", args=["-lc", (
            f'su postgres -c "dropdb --if-exists --force {db}"'
        )])

    async def _createdb(self, db: str, *, template: str = "template1") -> None:
        r = await self._sb.commands.run("bash", args=["-lc", (
            f'su postgres -c "createdb --template={template} {db}" && echo OK'
        )])
        if "OK" not in r.stdout:
            raise RuntimeError(f"createdb {db} failed:\n{r.stdout}\n{r.stderr}")

    async def _psql_file(self, db: str, name: str, sql: str) -> Tuple[bool, str]:
        path = f"/work/{name}"
        await self._sb.files.write(path, sql if sql.endswith("\n") else sql + "\n")
        # ON_ERROR_STOP: first error aborts non-zero.
        # statement_timeout: a hung statement is a failure, not a hang.
        cmd = (
            f'su postgres -c "psql -v ON_ERROR_STOP=1 -q '
            f"-c \\\"SET statement_timeout = {self._timeout_ms}\\\" "
            f'-f {path} {db}" 2>&1'
        )
        r = await self._sb.commands.run("bash", args=["-lc", cmd])
        return r.exitCode == 0, r.stdout.strip()


async def open_db(
    client,
    *,
    statement_timeout_ms: int = 10_000,
    from_snapshot: Optional[str] = None,
    sandbox_timeout_ms: int = 15 * 60 * 1000,
) -> Tuple["SandboxDb", object]:
    """Create a sandbox, boot postgres, and hand back (SandboxDb, sandbox).

    The caller owns the sandbox and must ``await sandbox.kill()`` when done.
    """
    kwargs = {"timeout_ms": sandbox_timeout_ms}
    if from_snapshot:
        kwargs["from_snapshot"] = from_snapshot
    else:
        kwargs["template"] = "base"
    sandbox = await client.create(**kwargs)
    await sandbox.connect()
    db = SandboxDb(sandbox, statement_timeout_ms=statement_timeout_ms)
    await db.boot()
    return db, sandbox
```
</details>

<details>
<summary><b><code>solari_db_review/reviewer.py</code></b></summary>

```python
"""The orchestrator: run every changed SQL file on a disposable Postgres and
report which ones don't finish cleanly.

    spec = fetch.from_fixture("fixtures/demo")
    res  = await review(spec, ReviewOptions(), solari_key)
    print(res.markdown)

The base schema (+ optional seed data) is loaded once into a `base_state`
template DB. Every changed file runs on a fresh ~0.5s *fork* of that template,
so all checks start from an identical known state no matter how big the seed
is.

This module only *detects* - it runs the SQL and captures the exact Postgres
error. Proposing a fix is a separate step: the opencode GitHub Action, which
edits the migration on the PR branch. The next CI run of this reviewer then
verifies that fix the same way.
"""
from __future__ import annotations

from .config import ReviewOptions, ReviewResult, ReviewSpec, StatementResult
from .report import render
from .sandbox_db import open_db


async def review(
    spec: ReviewSpec,
    opts: ReviewOptions,
    solari_key: str,
) -> ReviewResult:
    from solari_sandbox import SandboxClient

    result = ReviewResult(title=spec.title, pg_snapshot_id=opts.pg_snapshot_id)

    async with SandboxClient(api_key=solari_key, base_url="https://api.getsolari.com") as client:
        db, sandbox = await open_db(
            client,
            statement_timeout_ms=opts.statement_timeout_ms,
            from_snapshot=opts.pg_snapshot_id,
            sandbox_timeout_ms=opts.sandbox_timeout_ms,
        )
        result.sandbox_id = sandbox.sandboxId
        try:
            if not opts.pg_snapshot_id:
                try:
                    result.pg_snapshot_id = await db.snapshot("pg-base")
                except Exception:  # noqa: BLE001 - snapshotting is a nicety
                    pass

            base_ok, base_out = await db.load_base_state(spec.base_schema, spec.seed_data)
            if not base_ok:
                raise RuntimeError(f"base state failed to build:\n{base_out}")

            for change in spec.changes:
                await db.reset()
                ok, out = await db.run_sql(change.name, change.sql)
                result.statements.append(
                    StatementResult(
                        name=change.name, sql=change.sql, ok=ok,
                        error="" if ok else out,
                    )
                )
        finally:
            await sandbox.kill()

    result.markdown = render(result)
    return result
```
</details>

<details>
<summary><b><code>solari_db_review/report.py</code></b></summary>

```python
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

    if not result.all_ok:
        lines.append("---")
        lines.append("Comment `/oc fix` on this PR to have the opencode agent "
                     "propose a corrected migration; this check re-runs on its "
                     "commit and verifies it.")
        lines.append("")

    lines.append("---")
    lines.append("_Each file was run with `psql -v ON_ERROR_STOP=1` against the base "
                 "schema. \"Runs cleanly\" = every statement finished with no error._")
    return "\n".join(lines)


def _one(s: StatementResult) -> str:
    if s.ok:
        return f"### ✅ `{s.name}`\nRuns cleanly."
    return "\n".join([f"### ❌ `{s.name}`", "", "```", s.error.strip(), "```"])


def post_comment(pr: str, body: str) -> None:
    """Post the review to the PR via `gh pr comment`."""
    try:
        subprocess.run(["gh", "pr", "comment", pr, "--body", body],
                       capture_output=True, text=True, check=True)
    except FileNotFoundError:
        raise SystemExit("cannot post: the 'gh' CLI is not installed")
    except subprocess.CalledProcessError as e:
        raise SystemExit(f"gh pr comment failed:\n{e.stderr.strip()}")
```
</details>

<details>
<summary><b><code>solari_db_review/fetch.py</code></b></summary>

```python
"""Turn an input (a local fixture dir, or a GitHub PR) into a ReviewSpec.

Two paths, both simple:

  * ``from_fixture(dir)``  - read ``schema.sql`` + ``changes/*.sql`` from a folder.
    No GitHub, no auth. This is what the demo uses.

  * ``from_pr(url)``  - use the ``gh`` CLI to list the ``.sql`` files a PR changed
    and read their new contents, plus the base schema from the merge base.
    Needs ``gh`` installed and ``gh auth login`` done.
"""
from __future__ import annotations

import base64
import subprocess
from pathlib import Path
from typing import List

from .config import ReviewSpec, SqlFile


def from_fixture(fixture_dir: str) -> ReviewSpec:
    root = Path(fixture_dir)
    schema_path = root / "schema.sql"
    if not schema_path.exists():
        raise SystemExit(f"no schema.sql in {root}")
    changes_dir = root / "changes"
    files: List[SqlFile] = []
    for p in sorted(changes_dir.glob("*.sql")):
        files.append(SqlFile(name=p.name, sql=p.read_text(encoding="utf-8")))
    if not files:
        raise SystemExit(f"no changes/*.sql in {root}")
    seed_path = root / "seed.sql"  # optional
    return ReviewSpec(
        title=f"fixture: {root.name}",
        base_schema=schema_path.read_text(encoding="utf-8"),
        changes=files,
        seed_data=seed_path.read_text(encoding="utf-8") if seed_path.exists() else None,
    )


def from_pr(pr: str, schema_path: str = "schema.sql") -> ReviewSpec:
    """`pr` is a PR URL or number. `schema_path` is the repo path to the base
    schema file (the merge-base version is used)."""
    view = _gh(["pr", "view", pr, "--json", "title,headRefOid,baseRefOid"]).strip()
    import json
    meta = json.loads(view)

    changed = _gh(["pr", "diff", pr, "--name-only"]).splitlines()
    sql_files = [
        f for f in changed
        if f.strip().endswith(".sql") and f.strip() != schema_path
    ]
    if not sql_files:
        raise SystemExit(
            "this PR changes no .sql files (other than the base schema itself)"
        )

    changes = [
        SqlFile(name=path, sql=_read_file(path, meta["headRefOid"]))
        for path in sql_files
    ]
    base_schema = _read_file(schema_path, meta["baseRefOid"])
    return ReviewSpec(title=meta["title"], base_schema=base_schema, changes=changes)


def _read_file(path: str, ref: str) -> str:
    """Contents of a repo file at a given ref, via the GitHub contents API."""
    b64 = _gh(["api", f"repos/{{owner}}/{{repo}}/contents/{path}?ref={ref}",
               "-q", ".content"])
    return base64.b64decode(b64).decode("utf-8")


def _gh(args: List[str]) -> str:
    try:
        out = subprocess.run(["gh", *args], capture_output=True, text=True, check=True)
    except FileNotFoundError:
        raise SystemExit("the 'gh' CLI is not installed - use --fixtures instead, "
                         "or install https://cli.github.com")
    except subprocess.CalledProcessError as e:
        raise SystemExit(f"gh {' '.join(args)} failed:\n{e.stderr.strip()}")
    return out.stdout.strip()
```
</details>

### Top-level scripts

<details>
<summary><b><code>review_pr.py</code></b></summary>

```python
"""Review a database PR on a real, disposable postgres.

    # local fixture (no GitHub needed)
    python review_pr.py --fixtures fixtures/demo

    # a real GitHub PR (needs `gh auth login`)
    python review_pr.py https://github.com/owner/repo/pull/123 --schema db/schema.sql
    python review_pr.py 123 --schema db/schema.sql --comment

What it does: boots postgres in a Solari sandbox, loads the base schema (+
optional seed data) into a template DB, and runs each changed .sql file on a
fresh fork of it. Reports which files don't finish cleanly and the exact
Postgres error. Prints a Markdown review and writes it to output/. Exit code
is 0 if every change runs cleanly, 1 otherwise.

It does not fix anything - that's the opencode GitHub Action's job. This check
then re-runs on the fix commit and verifies it.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from solari_db_review import ReviewOptions, review
from solari_db_review.env import get, load_key
from solari_db_review.fetch import from_fixture, from_pr
from solari_db_review.report import post_comment


def _args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("pr", nargs="?", help="PR URL or number")
    p.add_argument("--fixtures", metavar="DIR", help="review a local fixture dir instead")
    p.add_argument("--schema", default="schema.sql",
                   help="repo path to the base schema file (PR mode; default schema.sql)")
    p.add_argument("--comment", action="store_true", help="post the review to the PR")
    return p.parse_args()


async def main() -> int:
    a = _args()
    if not a.fixtures and not a.pr:
        print("give a PR (url or number) or --fixtures DIR", file=sys.stderr)
        return 2

    spec = from_fixture(a.fixtures) if a.fixtures else from_pr(a.pr, a.schema)

    solari_key = load_key("SOLARI_API_KEY")
    opts = ReviewOptions(pg_snapshot_id=get("PG_SNAPSHOT_ID"))

    print(f"reviewing: {spec.title}  ({len(spec.changes)} changed .sql file(s))")
    print("booting postgres in a Solari sandbox ...\n")
    result = await review(spec, opts, solari_key)

    print(result.markdown)

    out_dir = Path(opts.out_dir)
    out_dir.mkdir(exist_ok=True)
    slug = (a.pr or Path(a.fixtures).name).replace("/", "_").replace(":", "")
    out_file = out_dir / f"{slug}-review.md"
    out_file.write_text(result.markdown, encoding="utf-8")
    print(f"\nwritten: {out_file}")

    if result.pg_snapshot_id and result.pg_snapshot_id != opts.pg_snapshot_id:
        print(f"tip: save PG_SNAPSHOT_ID={result.pg_snapshot_id} in .env to skip the "
              f"apt-get next time")

    if a.comment:
        if not a.pr:
            print("--comment needs a real PR (not --fixtures)", file=sys.stderr)
            return 2
        post_comment(a.pr, result.markdown)
        print("posted the review to the PR")

    return 0 if result.all_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
```
</details>

<details>
<summary><b><code>hello_world.py</code></b> (SDK smoke test)</summary>

```python
"""SDK smoke test: boot a Solari sandbox, start postgres, run SELECT 1.

    python hello_world.py

Prints the snapshot id at the end - put it in .env as PG_SNAPSHOT_ID so the
next run skips the one-time apt-get.
"""
from __future__ import annotations

import asyncio

from solari_db_review.env import load_key
from solari_db_review.sandbox_db import open_db


async def main() -> None:
    from solari_sandbox import SandboxClient

    key = load_key("SOLARI_API_KEY")
    async with SandboxClient(api_key=key, base_url="https://api.getsolari.com") as client:
        db, sandbox = await open_db(client)
        try:
            await db.load_base_state("CREATE TABLE ping (ok bool);")
            await db.reset()  # fork `review` from base_state
            ok, out = await db.run_sql("hello.sql", "INSERT INTO ping VALUES (true); SELECT * FROM ping;")
            print(f"fork + insert -> ok={ok}\n{out}")
            snap = await db.snapshot("pg-base")
            print(f"\nsnapshot: {snap}   (save as PG_SNAPSHOT_ID in .env)")
        finally:
            await sandbox.kill()


if __name__ == "__main__":
    asyncio.run(main())
```
</details>

### `fixtures/demo/` — the self-contained demo

<details>
<summary><b><code>fixtures/demo/schema.sql</code></b></summary>

```sql
-- Base schema the PR's changes run against.
CREATE TABLE users (
    id         SERIAL PRIMARY KEY,
    email      TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE orders (
    id         SERIAL PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users (id),
    total_cents INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO users (email) VALUES ('a@example.com'), ('b@example.com');
INSERT INTO orders (user_id, total_cents) VALUES (1, 1200), (1, 800), (2, 5000);
```
</details>

<details>
<summary><b><code>fixtures/demo/seed.sql</code></b> (optional; delete to check against an empty DB)</summary>

```sql
-- Optional representative data, loaded into the known state after schema.sql.
-- Every fork starts from this, so "runs cleanly" is checked against a
-- populated table, not an empty one.
INSERT INTO users (email)
SELECT 'user' || g || '@example.com'
FROM generate_series(3, 500) g;

INSERT INTO orders (user_id, total_cents)
SELECT (random() * 497 + 1)::int, (random() * 20000)::int
FROM generate_series(1, 5000);
```
</details>

<details>
<summary><b><code>fixtures/demo/changes/001_add_status_to_orders.sql</code></b> (clean — passes)</summary>

```sql
-- A clean migration: add a status column with a safe default.
ALTER TABLE orders ADD COLUMN status TEXT NOT NULL DEFAULT 'pending';

UPDATE orders SET status = 'paid' WHERE total_cents > 1000;
```
</details>

<details>
<summary><b><code>fixtures/demo/changes/002_orders_summary_view.sql</code></b> (broken — for the local demo)</summary>

```sql
-- A broken change: references a column that does not exist (orders.amount_cents
-- - the real column is total_cents) and groups by a missing users.name.
CREATE VIEW order_summary AS
SELECT u.name        AS customer,
       COUNT(o.id)   AS order_count,
       SUM(o.amount_cents) AS total_spent
FROM users u
JOIN orders o ON o.user_id = u.id
GROUP BY u.name;
```
</details>

> Leave `002` broken for the **local** `--fixtures` demo. For the **CI** demo
> you open a PR that adds a *new* broken migration (see §6) so the check has
> something fresh to flag.

---

## 2. GitHub Actions workflows

### `.github/workflows/db-review.yml`

```yaml
name: DB PR Review

# Runs the reviewer whenever a PR touches .sql files: boots postgres in a
# Solari sandbox, runs each changed file, and fails (red X) if any doesn't
# finish cleanly. It does NOT fix anything - comment `/oc fix` on the PR to
# have the opencode agent propose a correction (see opencode.yml); this check
# then re-runs on that commit and verifies it.
#
# Repo secret required: SOLARI_API_KEY
# Repo variable optional: PG_SNAPSHOT_ID (skips the one-time postgres install)
on:
  pull_request:
    paths:
      - "**/*.sql"

permissions:
  contents: read
  pull-requests: write   # for the --comment step

jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Review changed SQL
        env:
          SOLARI_API_KEY: ${{ secrets.SOLARI_API_KEY }}
          PG_SNAPSHOT_ID: ${{ vars.PG_SNAPSHOT_ID }}
          GH_TOKEN: ${{ github.token }}
        run: |
          # Point --schema at YOUR repo's real base schema file.
          python review_pr.py ${{ github.event.pull_request.number }} \
            --schema fixtures/demo/schema.sql \
            --comment

      - name: Upload review
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: db-review
          path: output/*.md
```

> **`--schema`**: this line assumes the base schema lives at
> `fixtures/demo/schema.sql`. In a real repo, change it to your actual schema
> file path (e.g. `db/schema.sql`). The reviewer reads that file at the PR's
> **merge-base** and runs the PR's changed `.sql` files against it.

### `.github/workflows/opencode.yml`

```yaml
name: opencode

# The fix agent. Triggered by commenting `/oc fix` (or `/opencode`) on a PR
# where the DB PR Review check failed. opencode reads the review comment,
# rewrites the broken migration on the PR branch, and pushes a commit.
#
# Repo secret required: OPENCODE_API_KEY   (your opencode.ai subscription key)
on:
  issue_comment:
    types: [created]
  pull_request_review_comment:
    types: [created]

jobs:
  opencode:
    if: |
      github.event.issue.pull_request != null &&
      (contains(github.event.comment.body, ' /oc') ||
       startsWith(github.event.comment.body, '/oc') ||
       contains(github.event.comment.body, ' /opencode') ||
       startsWith(github.event.comment.body, '/opencode'))
    runs-on: ubuntu-latest
    permissions:
      id-token: write
      contents: write        # push the fix commit
      pull-requests: write   # comment on the PR
      issues: write
    steps:
      - name: Checkout repository
        uses: actions/checkout@v6
        with:
          fetch-depth: 0
          # keep the GITHUB_TOKEN in git config so opencode can `git push`
          persist-credentials: true

      - name: Run opencode
        uses: anomalyco/opencode/github@latest
        env:
          OPENCODE_API_KEY: ${{ secrets.OPENCODE_API_KEY }}
          GITHUB_TOKEN: ${{ github.token }}
        with:
          use_github_token: true
          # Any model id from https://opencode.ai/docs/zen/ (opencode/<model>)
          # or opencode-go/<model> for the "Go" tier.
          model: opencode-go/qwen3.8-flash
          prompt: |
            A database-migration PR failed its "DB PR Review" check. That check
            ran each changed .sql file against the base schema on a real
            Postgres and one of them raised an error. The review comment on
            this PR contains, for each failing file: the filename, the exact
            SQL, and the Postgres ERROR message.

            Your task:
            1. Read the latest "DB PR review" comment on this PR to find which
               migration file failed and why.
            2. Read that migration file and the base schema it runs against
               (this repo's is fixtures/demo/schema.sql; a consuming repo's is
               named in the check's --schema argument).
            3. Fix the migration file so it runs cleanly against that schema,
               preserving its intent. Do NOT weaken it to silence the error -
               no dropping constraints, no deleting the failing statement, no
               "IF NOT EXISTS" to paper over a real conflict. Fix the cause
               (wrong column name, missing dependency, bad type, ordering).
            4. Commit only that migration file to this PR's branch with a
               message explaining the fix. Then leave a short PR comment
               summarising what was wrong and what you changed.

            The DB PR Review check re-runs automatically on your commit and
            will confirm the fix.
```

---

## 3. Test locally before pushing

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env: set SOLARI_API_KEY=slr_live_...   (leave PG_SNAPSHOT_ID blank)

# 1. smoke test - boots a VM, installs+starts postgres, forks, prints a snapshot id
python hello_world.py
#    -> copy the "snapshot: snap_xxxxx" line into .env as PG_SNAPSHOT_ID
#       so the next run skips the ~60s apt-get

# 2. full review of the bundled demo (detect only, no GitHub)
python review_pr.py --fixtures fixtures/demo
#    -> 001 passes, 002 fails with a column error, exit code 1
```

If both work, the core is sound.

---

## 4. Create the GitHub repo and push

```bash
git init
git add -A
git commit -m "solari-db-pr-reviewer: DB PR review on Solari + opencode fix agent"

# create the repo under your account and push (needs: gh auth login, workflow scope)
gh repo create <you>/my-db-pr-reviewer --public --source=. --remote=origin --push
```

If `gh repo create` complains it can't push workflow files:
`gh auth refresh -h github.com -s workflow` then `git push -u origin HEAD`.

If you made the repo in the web UI first (so it has a README/LICENSE):
```bash
git remote add origin https://github.com/<you>/my-db-pr-reviewer.git
git fetch origin
git rebase origin/main            # or origin/master
git push -u origin HEAD
```

---

## 5. Configure repo secrets and variables

```bash
R=<you>/my-db-pr-reviewer

# Secrets (Settings -> Secrets and variables -> Actions -> Secrets)
gh secret set SOLARI_API_KEY    --repo $R      # paste slr_live_...
gh secret set OPENCODE_API_KEY  --repo $R      # paste your opencode.ai sk-... key

# Variable (Settings -> Secrets and variables -> Actions -> Variables)
# Use the snapshot id hello_world.py printed in step 3 - skips the ~60s
# postgres install on every CI run.
gh variable set PG_SNAPSHOT_ID --body snap_xxxxx --repo $R
```

Verify:
```bash
gh secret list   --repo $R     # expect SOLARI_API_KEY, OPENCODE_API_KEY
gh variable list --repo $R     # expect PG_SNAPSHOT_ID
```

> **The `PG_SNAPSHOT_ID` variable is effectively required in practice.** Without
> it every CI run does `apt-get install postgresql` (~60s) inside the sandbox,
> and that long-running command is where the Solari control channel is most
> likely to drop. With it, `boot()` is ~2s.

---

## 6. Run the full loop on a demo PR

```bash
R=<you>/my-db-pr-reviewer

git checkout -b demo/fix-loop

# a NEW broken migration - references orders.placed_at, which doesn't exist
cat > fixtures/demo/changes/003_monthly_revenue.sql <<'SQL'
-- Intent: a materialized view of revenue per month + a recent-orders index.
-- Bug: `orders.placed_at` does not exist; the timestamp column is `created_at`.
CREATE MATERIALIZED VIEW monthly_revenue AS
SELECT date_trunc('month', placed_at) AS month,
       SUM(total_cents)               AS revenue_cents,
       COUNT(*)                       AS order_count
FROM orders
GROUP BY 1
ORDER BY 1;

CREATE INDEX idx_orders_recent ON orders (placed_at DESC);
SQL

git add fixtures/demo/changes/003_monthly_revenue.sql
git commit -m "Add monthly_revenue materialized view + recent-orders index"
git push -u origin demo/fix-loop

gh pr create --repo $R --base main --head demo/fix-loop \
  --title "Add monthly_revenue materialized view + recent-orders index" \
  --body "Demo: DB PR Review detects -> /oc fix -> opencode fixes -> re-check verifies."
```

**Watch it:**

```bash
# 1. DB PR Review runs automatically on the new PR
gh run watch $(gh run list --repo $R --workflow db-review.yml -L1 --json databaseId -q '.[0].databaseId') \
  --repo $R --exit-status
#    -> FAILS: "column \"placed_at\" does not exist", review posted as a PR comment

# 2. trigger the fixer
gh pr comment <PR#> --repo $R --body "/oc fix"

gh run watch $(gh run list --repo $R --workflow opencode.yml -L1 --json databaseId -q '.[0].databaseId') \
  --repo $R --exit-status
#    -> opencode reads the error, changes placed_at -> created_at in both the
#       view and the index, commits + pushes to demo/fix-loop, comments the PR

# 3. DB PR Review re-runs on opencode's commit. Because a bot pushed it, GitHub
#    marks the run "action_required" - approve it:
RUN=$(gh run list --repo $R --workflow db-review.yml --branch demo/fix-loop -L1 --json databaseId -q '.[0].databaseId')
gh api -X POST repos/$R/actions/runs/$RUN/approve
gh run watch $RUN --repo $R --exit-status
#    -> PASSES: "all changes run cleanly"
```

---

## 7. Known gotchas (all hit during the original build)

| Symptom | Cause | Fix |
|---|---|---|
| `git push` rejected: *"refusing to allow an OAuth App to create or update workflow"* | `gh` token lacks `workflow` scope | `gh auth refresh -h github.com -s workflow` |
| opencode run: `Environment variable "MODEL" is not set` | `anomalyco/opencode/github` **requires** a `model:` input | set `model:` in `opencode.yml` |
| opencode run: `GITHUB_TOKEN environment variable is not set` | `use_github_token: true` needs the token passed explicitly | add `GITHUB_TOKEN: ${{ github.token }}` under `env:` |
| opencode run: `fatal: could not read Username for 'https://github.com'` on push | `actions/checkout` ran with `persist-credentials: false` | set `persist-credentials: true` |
| opencode's fix commit **doesn't** re-trigger DB PR Review, or the run sits at **`action_required`** | GitHub does not auto-run workflows for commits pushed by `GITHUB_TOKEN` / a bot (loop-prevention) | approve manually (`gh api -X POST .../approve`), **or** push the fix with a fine-grained **PAT** (`contents: write`) stored as a secret instead of `GITHUB_TOKEN` — then re-checks fire automatically |
| Review step: `ConcurrencyLimitError: Too many concurrent sessions` | a previous run left a sandbox alive and your Solari plan's concurrency is low (was 1) | wait for the idle window, or kill leftovers (script below) |
| Review step: `NoCapacityError: No sandbox host available` | Solari's host pool is momentarily full (platform-side) | retry in a few minutes; consider adding retry/backoff to `open_db` |
| Review step: `ConnectionError: Control channel closed` during `boot()` | long-running `apt-get` over the WS control channel | set `PG_SNAPSHOT_ID` so `boot()` skips the install |
| PR "reviewed as a migration against itself" | the `--schema` file was also changed in the PR | `fetch.from_pr` already excludes the `--schema` path from the change set |
| Commit author shows as *"OpenCode"* with a stranger's name/avatar | opencode commits as `opencode@users.noreply.github.com`; GitHub renders an unrecognised email with a generic identity | cosmetic only — check `author_email`, it's the bot |

**Kill leftover Solari sandboxes:**

```bash
python - <<'PY'
import asyncio
from solari_db_review.env import load_key
async def main():
    from solari_sandbox import SandboxClient
    async with SandboxClient(api_key=load_key("SOLARI_API_KEY"),
                             base_url="https://api.getsolari.com") as c:
        for s in (await c.list()).get("sandboxes", []):
            print("killing", s.sandboxId[:40])
            await c.kill(s.sandboxId)
asyncio.run(main())
PY
```

**List / reuse Solari snapshots** (to get a `PG_SNAPSHOT_ID`):

```bash
python - <<'PY'
import asyncio
from solari_db_review.env import load_key
async def main():
    from solari_sandbox import SandboxClient
    async with SandboxClient(api_key=load_key("SOLARI_API_KEY"),
                             base_url="https://api.getsolari.com") as c:
        for s in await c.list_snapshots():
            print(s.id, s.name, f"{s.sizeBytes/1e6:.0f}MB", s.createdAt)
asyncio.run(main())
PY
```

---

## 8. Adapting to a real repo (not the demo)

1. Put your real base schema somewhere in the repo, e.g. `db/schema.sql`. It
   must be the full DDL to build an empty database.
2. In `db-review.yml`, change `--schema fixtures/demo/schema.sql` to
   `--schema db/schema.sql`.
3. (Optional) Add representative data: the reviewer looks for `seed.sql` only
   in the **fixture** path. For PR mode, fold seed data into the schema file,
   or extend `fetch.from_pr` to also fetch a `db/seed.sql`.
4. Keep your migrations wherever they already live — any `**/*.sql` file in a
   PR's diff is picked up (except the `--schema` file itself).
5. Drop the `fixtures/` folder if you don't want the bundled demo.

## 9. File checklist

```
my-db-pr-reviewer/
├── requirements.txt
├── .gitignore
├── .env.example
├── review_pr.py
├── hello_world.py
├── solari_db_review/
│   ├── __init__.py
│   ├── config.py
│   ├── env.py
│   ├── fetch.py
│   ├── report.py
│   ├── reviewer.py
│   └── sandbox_db.py
├── fixtures/demo/
│   ├── schema.sql
│   ├── seed.sql
│   └── changes/
│       ├── 001_add_status_to_orders.sql
│       └── 002_orders_summary_view.sql
├── .github/workflows/
│   ├── db-review.yml
│   └── opencode.yml
└── output/.gitkeep
```

Secrets: `SOLARI_API_KEY`, `OPENCODE_API_KEY`.  Variable: `PG_SNAPSHOT_ID`.

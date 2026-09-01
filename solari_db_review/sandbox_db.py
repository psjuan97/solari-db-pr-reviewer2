"""A disposable PostgreSQL inside a Solari sandbox.

One microVM, one postgres. Two databases live in it:

  * ``review``          - the authoritative one. The reviewer loads the base
                          schema here and runs a changed file against it. Its
                          exit code is the verdict.
  * ``review_scratch``  - a play area for the fix agent (see ``propose.py``).
                          Reset to the clean schema before every candidate.

The first ever run does a one-time ``apt-get install postgresql`` and takes a
snapshot; pass that id back (``ReviewOptions.pg_snapshot_id``) and later VMs
boot with postgres already there.

The check is simple: run a .sql file with ``psql -v ON_ERROR_STOP=1``. Exit 0
means every statement finished cleanly; non-zero means it raised, and psql
prints the ``ERROR:`` line.
"""
from __future__ import annotations

from typing import Optional, Tuple

REVIEW_DB = "review"
SCRATCH_DB = "review_scratch"

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
        self._schema_sql = ""  # remembered by load_schema, replayed on reset

    async def boot(self) -> None:
        await self._sb.files.mkdir("/work")
        r = await self._sb.commands.run("bash", args=["-lc", _BOOT])
        if "BOOT_OK" not in r.stdout:
            raise RuntimeError(f"postgres boot failed:\n{r.stdout}\n{r.stderr}")

    async def _recreate(self, db: str) -> None:
        r = await self._sb.commands.run("bash", args=["-lc", (
            f'su postgres -c "dropdb --if-exists {db}" && '
            f'su postgres -c "createdb {db}" && echo OK'
        )])
        if "OK" not in r.stdout:
            raise RuntimeError(f"recreate {db} failed:\n{r.stdout}\n{r.stderr}")

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

    # --- the reviewer's authoritative database ------------------------------

    async def load_schema(self, schema_sql: str) -> Tuple[bool, str]:
        """(Re)create `review`, load the base schema, and remember it."""
        self._schema_sql = schema_sql
        await self._recreate(REVIEW_DB)
        return await self._psql_file(REVIEW_DB, "_schema.sql", schema_sql)

    async def reset(self) -> Tuple[bool, str]:
        """Return `review` to the clean base-schema state."""
        await self._recreate(REVIEW_DB)
        return await self._psql_file(REVIEW_DB, "_schema.sql", self._schema_sql)

    async def run_sql(self, name: str, sql: str) -> Tuple[bool, str]:
        """Run a SQL blob against `review`. (ok, output)."""
        return await self._psql_file(REVIEW_DB, name, sql)

    # --- the fix agent's scratch database ----------------------------------

    async def try_on_scratch(self, sql: str) -> Tuple[bool, str]:
        """Reset `review_scratch` to the clean schema, then run `sql` on it.

        This is the tool the fix agent calls in its loop - one sandbox round
        trip per candidate.
        """
        await self._recreate(SCRATCH_DB)
        ok, out = await self._psql_file(SCRATCH_DB, "_scratch_schema.sql", self._schema_sql)
        if not ok:
            return False, f"(scratch schema load failed) {out}"
        return await self._psql_file(SCRATCH_DB, "_scratch.sql", sql)

    async def snapshot(self, name: str = "pg-base") -> str:
        return await self._sb.snapshot(name)


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

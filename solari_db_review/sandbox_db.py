"""A disposable PostgreSQL inside a Solari sandbox.

One microVM, one postgres, one database called ``review``. The first ever run
does a one-time ``apt-get install postgresql`` and takes a snapshot; pass that
snapshot id back on later runs (``ReviewOptions.pg_snapshot_id``) and the VM
boots with postgres already there.

The check the reviewer cares about is simple: run a .sql file with
``psql -v ON_ERROR_STOP=1``. Exit 0 means every statement in it finished
cleanly; non-zero means it raised - and psql prints the ``ERROR:`` line.
"""
from __future__ import annotations

from typing import Optional, Tuple

# Installs postgres (no-op if the snapshot already has it), starts the cluster,
# and creates an empty `review` database.
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
su postgres -c "dropdb --if-exists review"
su postgres -c "createdb review"
echo BOOT_OK
"""

# Drop and recreate `review` so each statement is checked from the same
# clean base-schema state, regardless of order.
_RESET = r"""
set -e
su postgres -c "dropdb --if-exists review"
su postgres -c "createdb review"
echo RESET_OK
"""


class SandboxDb:
    """Wraps a live Solari sandbox that is running postgres."""

    def __init__(self, sandbox, statement_timeout_ms: int = 10_000) -> None:
        self._sb = sandbox
        self._timeout_ms = statement_timeout_ms

    async def boot(self) -> None:
        await self._sb.files.mkdir("/work")
        r = await self._sb.commands.run("bash", args=["-lc", _BOOT])
        if "BOOT_OK" not in r.stdout:
            raise RuntimeError(f"postgres boot failed:\n{r.stdout}\n{r.stderr}")

    async def reset(self) -> None:
        """Return the database to an empty state."""
        r = await self._sb.commands.run("bash", args=["-lc", _RESET])
        if "RESET_OK" not in r.stdout:
            raise RuntimeError(f"db reset failed:\n{r.stdout}\n{r.stderr}")

    async def run_sql(self, name: str, sql: str) -> Tuple[bool, str]:
        """Run one SQL blob against `review`. Returns (ok, output).

        ok is True iff psql exited 0 (every statement finished with no error).
        output is psql's combined stdout+stderr - the ERROR line when it failed.
        """
        path = f"/work/{name}"
        await self._sb.files.write(path, sql if sql.endswith("\n") else sql + "\n")
        # ON_ERROR_STOP: first error aborts with a non-zero exit.
        # statement_timeout: a hung statement counts as a failure, not a hang.
        cmd = (
            f'su postgres -c "psql -v ON_ERROR_STOP=1 -q '
            f"-c \\\"SET statement_timeout = {self._timeout_ms}\\\" "
            f'-f {path} review" 2>&1'
        )
        r = await self._sb.commands.run("bash", args=["-lc", cmd])
        return r.exitCode == 0, r.stdout.strip()

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

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

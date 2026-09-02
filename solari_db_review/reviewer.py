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

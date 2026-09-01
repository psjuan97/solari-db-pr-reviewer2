"""The orchestrator: verify every changed file, propose + re-check fixes, report.

    spec  = fetch.from_fixture("fixtures/demo")
    res   = await review(spec, ReviewOptions(), solari_key, anthropic_key)
    print(res.markdown)
"""
from __future__ import annotations

from typing import Optional

from .config import ReviewOptions, ReviewResult, ReviewSpec, StatementResult
from .propose import propose_fix
from .report import render
from .sandbox_db import open_db


async def review(
    spec: ReviewSpec,
    opts: ReviewOptions,
    solari_key: str,
    anthropic_key: str,
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
            # Take a snapshot the first time (no snapshot was supplied) so the
            # next run can skip the apt-get.
            if not opts.pg_snapshot_id:
                try:
                    result.pg_snapshot_id = await db.snapshot("pg-base")
                except Exception:  # noqa: BLE001 - snapshotting is a nicety
                    pass

            for change in spec.changes:
                result.statements.append(
                    await _check_one(db, spec.base_schema, change, opts, anthropic_key)
                )
        finally:
            await sandbox.kill()

    result.markdown = render(result)
    return result


async def _check_one(db, base_schema, change, opts, anthropic_key) -> StatementResult:
    # Fresh base-schema state for this file.
    await db.reset()
    schema_ok, schema_out = await db.run_sql("_schema.sql", base_schema)
    if not schema_ok:
        return StatementResult(
            name=change.name, sql=change.sql, ok=False,
            error=f"base schema failed to load:\n{schema_out}",
        )

    ok, out = await db.run_sql(change.name, change.sql)
    if ok:
        return StatementResult(name=change.name, sql=change.sql, ok=True)

    res = StatementResult(name=change.name, sql=change.sql, ok=False, error=out)

    # Broken - ask Claude, then re-check the candidate from the same clean state.
    try:
        fix, rationale = propose_fix(
            schema=base_schema, name=change.name, sql=change.sql, error=out,
            model=opts.model, api_key=anthropic_key,
        )
    except Exception as e:  # noqa: BLE001 - a fix is best-effort, never fatal
        res.fix_rationale = f"(could not reach Claude: {e})"
        return res
    if not fix:
        return res
    res.proposed_fix, res.fix_rationale = fix, rationale

    await db.reset()
    await db.run_sql("_schema.sql", base_schema)
    res.fix_ok, _ = await db.run_sql(f"fix_{change.name}", fix)
    return res

"""The orchestrator: verify every changed file, agentically fix failures, report.

    spec = fetch.from_fixture("fixtures/demo")
    res  = await review(spec, ReviewOptions(), solari_key, anthropic_key)
    print(res.markdown)

For a failing file the fix agent (see propose.py) runs a debug loop against a
scratch database in the same sandbox. Whatever it returns is then re-checked
against the authoritative `review` database - that result, not the agent's
word, is what the review reports.
"""
from __future__ import annotations

import asyncio

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
            if not opts.pg_snapshot_id:
                try:
                    result.pg_snapshot_id = await db.snapshot("pg-base")
                except Exception:  # noqa: BLE001 - snapshotting is a nicety
                    pass

            schema_ok, schema_out = await db.load_schema(spec.base_schema)
            if not schema_ok:
                raise RuntimeError(f"base schema failed to load:\n{schema_out}")

            for change in spec.changes:
                result.statements.append(
                    await _check_one(db, spec, change, opts, anthropic_key)
                )
        finally:
            await sandbox.kill()

    result.markdown = render(result)
    return result


async def _check_one(db, spec, change, opts, anthropic_key) -> StatementResult:
    await db.reset()
    ok, out = await db.run_sql(change.name, change.sql)
    if ok:
        return StatementResult(name=change.name, sql=change.sql, ok=True)

    res = StatementResult(name=change.name, sql=change.sql, ok=False, error=out)

    # --- hand off to the fix agent -----------------------------------------
    # Its run_sql tool is synchronous (the anthropic SDK is); bridge each call
    # back onto this event loop so it hits the real sandbox.
    loop = asyncio.get_running_loop()

    def verify(candidate_sql: str):
        fut = asyncio.run_coroutine_threadsafe(db.try_on_scratch(candidate_sql), loop)
        return fut.result()

    try:
        fix, rationale, trace = await asyncio.to_thread(
            propose_fix,
            schema=spec.base_schema, name=change.name, sql=change.sql, error=out,
            model=opts.model, api_key=anthropic_key, verify=verify,
            max_iters=opts.max_fix_iters,
        )
    except Exception as e:  # noqa: BLE001 - a fix is best-effort, never fatal
        res.fix_rationale = f"(fix agent errored: {e})"
        return res

    res.fix_iters = trace.iters
    if not fix:
        res.fix_rationale = rationale or "(agent proposed no fix)"
        return res
    res.proposed_fix, res.fix_rationale = fix, rationale

    # Authoritative re-check on `review`, from the clean base-schema state.
    await db.reset()
    res.fix_ok, _ = await db.run_sql(f"fix_{change.name}", fix)
    return res

"""Agentically fix a SQL file that postgres rejected.

Claude is given the base schema, the failing SQL, the exact error, and a single
tool - ``run_sql`` - wired to a *scratch* database in the same Solari sandbox.
It iterates: try a candidate, read the real postgres error, revise, try again,
up to ``max_iters`` times. It stops when a candidate runs clean, then reports
that candidate.

Nothing here decides the fix is good for real: the caller re-runs the returned
SQL against the actual ``review`` database as the final gate. This module just
lets the model *debug against a database* instead of guessing from a string.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable, List, Tuple

# A scratch DB the agent may hammer freely, kept separate from `review` (the
# one the caller uses for the authoritative re-check).
SCRATCH_DB = "review_scratch"

_SYSTEM = f"""You fix broken PostgreSQL migrations.

You get: the database schema, a SQL file that failed, and the exact postgres
error. Your job is to produce corrected SQL that runs cleanly against that
schema while preserving the original intent.

Rules:
- Do NOT weaken the migration to make the error go away: no dropping
  constraints, no deleting the failing statement, no CREATE ... IF NOT EXISTS
  to paper over a real conflict. Fix the actual cause.
- You have one tool, run_sql, that runs SQL against a scratch database
  ({SCRATCH_DB}) which starts as a fresh copy of the schema. Use it to test
  candidates. The scratch DB is reset to the clean schema before each call.
- Iterate until run_sql reports ok=true, then call submit_fix with your final
  SQL and a one-sentence rationale. If you cannot fix it, call submit_fix with
  an empty fix and say why in the rationale.
"""

_TOOLS = [
    {
        "name": "run_sql",
        "description": (
            "Run SQL against the scratch database (reset to the clean schema "
            "first). Returns {ok, output} - output is psql's stderr/stdout, "
            "i.e. the ERROR line when it failed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"sql": {"type": "string"}},
            "required": ["sql"],
        },
    },
    {
        "name": "submit_fix",
        "description": "Report your final answer and stop.",
        "input_schema": {
            "type": "object",
            "properties": {
                "fix": {"type": "string", "description": "the full corrected SQL, or empty if unfixable"},
                "rationale": {"type": "string", "description": "one short sentence"},
            },
            "required": ["fix", "rationale"],
        },
    },
]

_USER = """-- SCHEMA --
{schema}

-- FAILING SQL ({name}) --
{sql}

-- POSTGRES ERROR --
{error}
"""


@dataclass
class ProposeTrace:
    """What the agent did, for the review's transparency."""

    iters: int = 0
    attempts: List[Tuple[str, bool]] = field(default_factory=list)  # (sql, ok)


# A verifier callable: given SQL, run it against the scratch DB from a clean
# schema state and return (ok, output). Injected by the caller so this module
# never touches the sandbox directly.
Verifier = Callable[[str], Tuple[bool, str]]


def propose_fix(
    *,
    schema: str,
    name: str,
    sql: str,
    error: str,
    model: str,
    api_key: str,
    verify: Verifier,
    max_iters: int = 6,
) -> Tuple[str, str, ProposeTrace]:
    """Return (fix_sql, rationale, trace). fix_sql is '' if the agent gave up."""
    from anthropic import Anthropic

    client = Anthropic(api_key=api_key)
    trace = ProposeTrace()
    messages: List[dict] = [{
        "role": "user",
        "content": _USER.format(schema=schema, name=name, sql=sql, error=error),
    }]

    for _ in range(max_iters):
        msg = client.messages.create(
            model=model, max_tokens=2048, system=_SYSTEM, tools=_TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": msg.content})

        tool_uses = [b for b in msg.content if b.type == "tool_use"]
        if not tool_uses:
            # No tool call and not done - nudge once, then bail.
            messages.append({"role": "user", "content": "Call run_sql or submit_fix."})
            continue

        results = []
        for tu in tool_uses:
            if tu.name == "submit_fix":
                fix = str(tu.input.get("fix", "")).strip()
                rationale = str(tu.input.get("rationale", "")).strip()
                return fix, rationale, trace

            # run_sql
            candidate = str(tu.input.get("sql", ""))
            ok, output = verify(candidate)
            trace.iters += 1
            trace.attempts.append((candidate, ok))
            results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": json.dumps({"ok": ok, "output": output[:4000]}),
            })
        messages.append({"role": "user", "content": results})

    # Ran out of iterations. Return the last candidate that ran clean, if any.
    for candidate, ok in reversed(trace.attempts):
        if ok:
            return candidate, "(hit iteration limit; returning last clean candidate)", trace
    return "", "(hit iteration limit without a clean candidate)", trace

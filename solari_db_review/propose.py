"""Ask Claude to fix a SQL statement that postgres rejected.

Input: the base schema DDL, the failing SQL, and the exact postgres error.
Output: a single corrected SQL statement + a one-line rationale.

The caller re-runs the candidate in the sandbox - this module never decides a
fix is good, it only proposes one.
"""
from __future__ import annotations

import json
from typing import Tuple

_SYSTEM = (
    "You fix broken PostgreSQL. You are given the database schema, a SQL file "
    "that failed, and the exact error. Return corrected SQL that will run "
    "cleanly against that schema. Keep the intent of the original. Return ONLY "
    "JSON: {\"fix\": \"<the full corrected SQL>\", \"rationale\": \"<one short "
    "sentence>\"}. No markdown, no prose outside the JSON."
)

_USER = """-- SCHEMA --
{schema}

-- FAILING SQL ({name}) --
{sql}

-- POSTGRES ERROR --
{error}
"""


def propose_fix(
    *, schema: str, name: str, sql: str, error: str, model: str, api_key: str
) -> Tuple[str, str]:
    """Return (fix_sql, rationale). ('', '') if Claude gave nothing usable."""
    from anthropic import Anthropic

    client = Anthropic(api_key=api_key)
    msg = client.messages.create(
        model=model,
        max_tokens=1024,
        system=_SYSTEM,
        messages=[{
            "role": "user",
            "content": _USER.format(schema=schema, name=name, sql=sql, error=error),
        }],
    )
    text = "".join(b.text for b in msg.content if b.type == "text").strip()
    try:
        data = json.loads(text)
        return str(data.get("fix", "")).strip(), str(data.get("rationale", "")).strip()
    except (json.JSONDecodeError, AttributeError):
        return "", ""

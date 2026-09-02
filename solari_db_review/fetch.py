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

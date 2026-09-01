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

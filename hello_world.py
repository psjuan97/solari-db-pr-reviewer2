"""SDK smoke test: boot a Solari sandbox, start postgres, run SELECT 1.

    python hello_world.py

Prints the snapshot id at the end - put it in .env as PG_SNAPSHOT_ID so the
next run skips the one-time apt-get.
"""
from __future__ import annotations

import asyncio

from solari_db_review.env import load_key
from solari_db_review.sandbox_db import open_db


async def main() -> None:
    from solari_sandbox import SandboxClient

    key = load_key("SOLARI_API_KEY")
    async with SandboxClient(api_key=key, base_url="https://api.getsolari.com") as client:
        db, sandbox = await open_db(client)
        try:
            ok, out = await db.run_sql("hello.sql", "SELECT 1 AS one;")
            print(f"SELECT 1 -> ok={ok}\n{out}")
            snap = await db.snapshot("pg-base")
            print(f"\nsnapshot: {snap}   (save as PG_SNAPSHOT_ID in .env)")
        finally:
            await sandbox.kill()


if __name__ == "__main__":
    asyncio.run(main())

"""Review a database PR on a real, disposable postgres.

    # local fixture (no GitHub needed)
    python review_pr.py --fixtures fixtures/demo

    # a real GitHub PR (needs `gh auth login`)
    python review_pr.py https://github.com/owner/repo/pull/123 --schema db/schema.sql
    python review_pr.py 123 --schema db/schema.sql --comment

What it does: boots postgres in a Solari sandbox, loads the base schema (+
optional seed data) into a template DB, and runs each changed .sql file on a
fresh fork of it. Reports which files don't finish cleanly and the exact
Postgres error. Prints a Markdown review and writes it to output/. Exit code
is 0 if every change runs cleanly, 1 otherwise.

It does not fix anything - that's the opencode GitHub Action's job. This check
then re-runs on the fix commit and verifies it.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from solari_db_review import ReviewOptions, review
from solari_db_review.env import get, load_key
from solari_db_review.fetch import from_fixture, from_pr
from solari_db_review.report import post_comment


def _args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("pr", nargs="?", help="PR URL or number")
    p.add_argument("--fixtures", metavar="DIR", help="review a local fixture dir instead")
    p.add_argument("--schema", default="schema.sql",
                   help="repo path to the base schema file (PR mode; default schema.sql)")
    p.add_argument("--comment", action="store_true", help="post the review to the PR")
    return p.parse_args()


async def main() -> int:
    a = _args()
    if not a.fixtures and not a.pr:
        print("give a PR (url or number) or --fixtures DIR", file=sys.stderr)
        return 2

    spec = from_fixture(a.fixtures) if a.fixtures else from_pr(a.pr, a.schema)

    solari_key = load_key("SOLARI_API_KEY")
    opts = ReviewOptions(pg_snapshot_id=get("PG_SNAPSHOT_ID"))

    print(f"reviewing: {spec.title}  ({len(spec.changes)} changed .sql file(s))")
    print("booting postgres in a Solari sandbox ...\n")
    result = await review(spec, opts, solari_key)

    print(result.markdown)

    out_dir = Path(opts.out_dir)
    out_dir.mkdir(exist_ok=True)
    slug = (a.pr or Path(a.fixtures).name).replace("/", "_").replace(":", "")
    out_file = out_dir / f"{slug}-review.md"
    out_file.write_text(result.markdown, encoding="utf-8")
    print(f"\nwritten: {out_file}")

    if result.pg_snapshot_id and result.pg_snapshot_id != opts.pg_snapshot_id:
        print(f"tip: save PG_SNAPSHOT_ID={result.pg_snapshot_id} in .env to skip the "
              f"apt-get next time")

    if a.comment:
        if not a.pr:
            print("--comment needs a real PR (not --fixtures)", file=sys.stderr)
            return 2
        post_comment(a.pr, result.markdown)
        print("posted the review to the PR")

    return 0 if result.all_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

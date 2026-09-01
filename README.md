# solari-db-pr-reviewer — review database PRs on a real, disposable Postgres

An agent for **database pull requests**, built on [Solari](https://getsolari.com).

A PR changes some SQL — a migration, a view, a query. This tool boots a
throwaway PostgreSQL in a **Solari sandbox**, loads the branch's base schema,
and runs each changed `.sql` file against it. For any file that doesn't finish
cleanly, a **fix agent** debugs it in a loop — try a candidate, read the real
Postgres error, revise, retry — against a scratch database in the *same*
sandbox. Whatever it lands on is re-checked against the authoritative database,
and that result (not the agent's word) goes into a Markdown review that can be
posted back to the PR.

```
 ┌───────────┐  changed *.sql + base schema  ┌──────────────┐  per-file ok / error  ┌──────────────┐
 │   FETCH   │ ────────────────────────────► │   VERIFY     │ ────────────────────► │    REPORT    │
 │ gh CLI    │                               │ postgres in  │   ▲  fix re-verified   │ Markdown +   │
 │ or a dir  │                               │ a Solari VM  │   │                     │ gh pr comment│
 └───────────┘                               └──────────────┘   │                     └──────────────┘
                                               │  ┌────────────┴─────────────┐
                                       failure │  │        FIX AGENT         │  Claude + a run_sql tool,
                                               └─►│  loop: try → error →     │  looping against a scratch
                                                  │  revise → try (≤N times) │  DB in the same sandbox
                                                  └──────────────────────────┘
```

## Why Solari

Every PR gets its **own real database** in a microVM that boots in about a
second and is thrown away after. Untrusted SQL runs with no shared CI database
to corrupt and nothing to clean up. The one-time `apt-get install postgresql`
is captured with `sandbox.snapshot()`; from the second run on, the VM boots
with Postgres already there.

The check is deliberately simple, and matches what "does this PR work" usually
means in practice: each file is run with `psql -v ON_ERROR_STOP=1` against a
freshly reset copy of the base schema. **It runs cleanly** = every statement
finished with no error, under a `statement_timeout`. It does not judge query
*semantics* — only that it executes and finishes.

The fix agent gets its own `review_scratch` database in the same VM, reset to
the clean schema before every candidate, so it can experiment without touching
the authoritative result. It's told not to weaken the migration (no dropping
constraints, no deleting the failing statement) — fix the cause, don't hide it.
Its debug loop is capped by `ReviewOptions.max_fix_iters` (default 6).

## Quickstart

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env      # then fill in SOLARI_API_KEY and ANTHROPIC_API_KEY

# review the bundled demo — no GitHub needed
python review_pr.py --fixtures fixtures/demo
```

The demo fixture has one clean migration and one broken view (it references
columns that don't exist). Expected output:

```
## DB PR review — fixture: demo

**❌ some changes fail** · checked on a disposable PostgreSQL in a Solari sandbox

### ✅ `001_add_status_to_orders.sql`
Runs cleanly.

### ❌ `002_orders_summary_view.sql`
    ERROR:  column u.name does not exist

**Proposed fix** (✅ verified — runs cleanly, 2 attempt(s))
> Use users.email and orders.total_cents; the referenced names do not exist.
    CREATE VIEW order_summary AS ...
```

The review is also written to `output/<name>-review.md`.

### Reviewing a real GitHub PR

Needs the [`gh` CLI](https://cli.github.com) and `gh auth login`.

```bash
python review_pr.py https://github.com/owner/repo/pull/123 --schema db/schema.sql
python review_pr.py 123 --schema db/schema.sql --comment   # also posts the review
```

`--schema` is the repo path to the base schema file; its merge-base version is
what the changes run against. `--comment` posts the Markdown review to the PR
via `gh pr comment` — nothing is posted without that flag.

### First run is slower

The first run installs Postgres in the sandbox (~60s) and prints a snapshot id.
Put it in `.env` as `PG_SNAPSHOT_ID=` and later runs boot straight from it.

## Repo layout

```
solari_db_review/
├── config.py       ReviewSpec, ReviewOptions, StatementResult, ReviewResult (dataclasses)
├── env.py          tiny .env reader (no dependency)
├── fetch.py        input → ReviewSpec:  from_fixture(dir)  |  from_pr(url) via gh CLI
├── sandbox_db.py   boot a Solari sandbox, install+start postgres; `review` DB (verdict)
│                   + `review_scratch` DB (the fix agent's play area)
├── propose.py      the fix agent: Claude + a run_sql tool, looping against the scratch DB
├── report.py       render Markdown; post_comment() via gh
└── reviewer.py     orchestrator: for each file → verify → (fix agent) → re-verify → report

review_pr.py        the CLI
hello_world.py      SDK smoke test: boot sandbox, start postgres, SELECT 1
fixtures/demo/      schema.sql + changes/ (one good, one broken)
```

## What it leans on in the Solari SDK

- **`SandboxClient.create(...)` / `from_snapshot=`** — a microVM per review,
  optionally booted from a snapshot that already has Postgres.
- **`sandbox.commands.run("bash", args=[...])`** — install and drive Postgres
  (`pg_ctlcluster`, `psql`). Commands are not shell-interpreted, so SQL is
  written to a file with `sandbox.files.write` and run with `psql -f`.
- **`sandbox.snapshot(name)`** — capture the installed-Postgres VM once so it
  never has to be installed again.
- **`sandbox.kill()`** — the review owns exactly one VM and destroys it in a
  `finally`, never relying on the idle timeout.

## License

MIT

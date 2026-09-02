# solari-db-pr-reviewer — review database PRs on a real, disposable Postgres

Two agents for **database pull requests**:

- **The reviewer** ([Solari](https://getsolari.com)) — boots a throwaway
  PostgreSQL in a microVM, loads the branch's base schema, and runs each
  changed `.sql` file on a fresh fork of it. Reports which files don't finish
  cleanly, with the exact Postgres error. It only *detects*.
- **The fixer** ([opencode](https://opencode.ai) GitHub Action) — triggered by
  a `/oc fix` comment on a failing PR. Reads the reviewer's error, rewrites the
  broken migration on the PR branch, and pushes a commit. The reviewer then
  re-runs on that commit and verifies the fix.

```
  PR touches *.sql
        │
        ▼
 ┌──────────────┐   ❌ file X fails:        ┌───────────────┐  pushes fixed X    ┌──────────────┐
 │  DB PR Review│   "column ... does not   │ opencode agent│  to the PR branch  │  DB PR Review│
 │  (Solari VM) │──► exist" + the SQL   ──►│  (/oc fix)    │───────────────────►│  re-runs  ✅ │
 └──────────────┘   posted as a comment    └───────────────┘                    └──────────────┘
   detect                                    propose                              verify
```

## Why Solari

Every PR gets its **own real database** in a microVM that boots in about a
second and is thrown away after. Untrusted SQL runs with no shared CI database
to corrupt and nothing to clean up. The one-time `apt-get install postgresql`
is captured with `sandbox.snapshot()`; from the second run on, the VM boots
with Postgres already there.

The check is deliberately simple, and matches what "does this PR work" usually
means in practice: each file is run with `psql -v ON_ERROR_STOP=1`. **It runs
cleanly** = every statement finished with no error, under a `statement_timeout`.
It does not judge query *semantics* — only that it executes and finishes.

**Snapshot-and-fork.** The base schema — plus an optional `seed.sql` of
representative data — is loaded **once** into a `base_state` template database.
Every changed file then runs on a fresh **fork** of it:

```
createdb --template=base_state review     # ~0.5s, and flat regardless of data size
```

`base_state` is never written to, so every check starts from an identical known
state, and forking stays fast even when that state is a realistic dump rather
than three empty tables.

## Quickstart (local, no GitHub)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env      # then fill in SOLARI_API_KEY

# review the bundled demo
python review_pr.py --fixtures fixtures/demo
```

The demo fixture has one clean migration and two broken ones. Expected output:

```
## DB PR review — fixture: demo

**❌ some changes fail** · checked on a disposable PostgreSQL in a Solari sandbox

### ✅ `001_add_status_to_orders.sql`
Runs cleanly.

### ❌ `002_orders_summary_view.sql`
    ERROR:  column u.name does not exist

### ❌ `003_add_orders_index.sql`
    ERROR:  column "placed_at" does not exist
```

The review is also written to `output/<name>-review.md`, and `review_pr.py`
exits non-zero when any change fails.

### Reviewing a real GitHub PR from the CLI

Needs the [`gh` CLI](https://cli.github.com) and `gh auth login`.

```bash
python review_pr.py 123 --schema db/schema.sql            # print the review
python review_pr.py 123 --schema db/schema.sql --comment  # also post it to the PR
```

`--schema` is the repo path to the base schema file; its merge-base version is
what the changes run against.

## Wiring it into CI (the two-workflow flow)

| Workflow | Trigger | Does |
|---|---|---|
| [`db-review.yml`](.github/workflows/db-review.yml)  | PR touches `**/*.sql` | boots the Solari VM, runs each changed file, comments the result, fails the check if any file errors |
| [`opencode.yml`](.github/workflows/opencode.yml)    | `/oc fix` comment on a PR | opencode rewrites the failing migration on the PR branch and pushes a commit |

Setup in the repo you want reviewed:

1. Copy `solari_db_review/`, `review_pr.py`, `requirements.txt`, and both
   workflow files into it.
2. **Settings → Secrets and variables → Actions → Secrets**: add
   - `SOLARI_API_KEY` — your Solari key
   - `OPENCODE_API_KEY` — your opencode.ai subscription key
3. Edit `db-review.yml`'s `--schema` argument to point at your repo's real base
   schema file (this repo uses `fixtures/demo/schema.sql`; the default is
   `schema.sql` at the root).
4. After the first `db-review` run, its log prints a snapshot id
   (`tip: save PG_SNAPSHOT_ID=...`). Add it under **Variables** (not Secrets)
   as `PG_SNAPSHOT_ID` — later runs then skip the ~60s Postgres install.
5. Open a PR that touches a `.sql` file. If it fails, comment `/oc fix`. When
   opencode pushes its commit, `db-review` re-runs and (if the fix is good)
   goes green. A human still reviews and merges.

`db-review.yml` also uploads the Markdown review as a build artifact
(**Actions → the run → Artifacts → db-review**).

## Repo layout

```
solari_db_review/
├── config.py       ReviewSpec, ReviewOptions, StatementResult, ReviewResult (dataclasses)
├── env.py          tiny .env reader (no dependency)
├── fetch.py        input → ReviewSpec:  from_fixture(dir)  |  from_pr(url) via gh CLI
├── sandbox_db.py   boot a Solari sandbox, install+start postgres; load base_state
│                   once, then fork `review` per changed file
├── report.py       render Markdown; post_comment() via gh
└── reviewer.py     orchestrator: for each file → run on a fork → record ok / error

review_pr.py        the CLI
hello_world.py      SDK smoke test: boot sandbox, start postgres, fork + insert
fixtures/demo/      schema.sql + seed.sql (optional) + changes/ (one good, two broken)
.github/workflows/  db-review.yml (detect)  +  opencode.yml (fix)
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

Inside that one VM, per-file isolation is Postgres `CREATE DATABASE ...
TEMPLATE` (a fork of `base_state`), not a fresh sandbox per file — same
guarantee, ~0.5s, no extra control channels to manage.

## License

MIT

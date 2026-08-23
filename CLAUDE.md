# my-agents

Orchestration system: a poll loop on a VPS claims specs from Notion and dispatches them to
headless worker sessions, which turn each spec into a pull request.

**You are probably a worker session.** You were started by `worker.py` with a spec as your
brief, inside a fresh shallow clone on a new branch. You have no memory of previous sessions.

## What you must not do

- **Do not `git push`, and do not open a pull request.** The worker does both after you
  finish. If you push, you will collide with it.
- **Do not commit secrets.** Tokens arrive through the environment from Infisical. Never
  write a token into a file, a test fixture, a log line, or a commit message. Never add a
  `.env`.
- **Do not touch the running system while implementing a spec** — `/etc/systemd/system/orchestrator.service`,
  `/etc/orchestrator.env`, `/opt/orchestrator`, or anything under `/root`. You are editing a
  checkout, not the deployment.
- **Do not add dependencies.** See below.

## What to do when the spec is unclear

Choose the smaller interpretation, implement that, and say what you assumed in your final
message. If the spec cannot be done at all, change nothing and explain why — the worker
treats "no changes" as a failed attempt, which is the correct outcome. Never invent scope
the spec did not ask for.

## Code conventions

- **Standard library only.** `orchestrator.py` and `worker.py` have no third-party imports
  and none should be added. HTTP is `urllib.request`, state is `sqlite3`, config is
  environment variables. Adding `requests` for a single call is not an improvement.
- **Config comes from the environment**, never a config file, and never a literal in code.
- **Small diffs.** Change what the spec asks for and nothing adjacent. Tidying unrelated code
  makes the pull request harder to review and is not free.
- Match the surrounding style: module-level constants read from `os.environ`, short
  functions, comments that explain *why* rather than restating the code.

## Tests

Assert-based self-checks, no framework, no fixtures:

```bash
python3 test_orchestrator.py    # claim/lease logic
python3 test_worker.py          # spec parsing, branch naming, brief guardrails
```

Run the relevant one before finishing. If you change claiming, leasing, retry behaviour, or
spec parsing, add a case — those are the paths that silently lose work when wrong. Trivial
changes do not need a test.

## Repository layout

| File | What it is |
|---|---|
| `orchestrator.py` | Poll loop: pull specs from Notion, claim one with a lease, dispatch, write status back |
| `worker.py` | One spec → assess size → (split, or implement and open a PR) |
| `test_orchestrator.py`, `test_worker.py` | Self-checks |
| `CONTRIBUTING.md` | Branching and secrets rules |

## Concepts worth knowing before changing the orchestrator

- **A claim carries a lease** — owner, expiry, attempt count. A spec is claimed *before*
  dispatch so two workers can never take the same one.
- **Lease expiry is not failure.** An expired lease is reclaimed with `attempts + 1`. Only
  exhausting `MAX_ATTEMPTS` marks a spec `failed`.
- **Deterministic failures are not retried.** A timeout re-runs identically on the same
  budget, so it goes straight to `failed`. Retries are for transient failures only.
- **A spec is one session's work.** Anything larger is split into child specs before any
  code is written.
- **Notion is the source of truth**, not this repository. Specs, status, and approvals all
  live there. Workers never write to Notion — the orchestrator alone does.

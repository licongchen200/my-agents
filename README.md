# my-agents

A persistent orchestrator agent system for coordinating automated coding work across a VPS and a local Mac.

This repository holds the **worker code and deploy pipeline**. Specifications live in Notion — see the [my agents](https://app.notion.com/p/3c4ac87aab4481b099dcd02c3a04d749) page tree, which is the source of truth for architecture, specs, and task status.

## Overview

An **orchestrator** runs persistently on my VPS and coordinates two types of **worker agents**:

- **Backend workers** — run on the VPS or a cloud VM, for general backend tasks.
- **Mac workers** — run on my Mac, dedicated to iOS builds, since Xcode is required to compile those and only runs on macOS.

## How it works

1. **Task creation** — I write task specifications during chat sessions; the talking agent writes them into the Notion page tree.
2. **Polling** — the orchestrator polls Notion for new or updated specs.
3. **Dispatch** — each task is routed to the appropriate worker type based on the task's declared type (backend vs. iOS/Xcode). Workers do not poll Notion themselves.
4. **Execution** — workers execute their assigned tasks and commit results back to git, reporting status to the orchestrator.
5. **Status** — the orchestrator alone updates spec status in Notion and posts notifications to Slack.
6. **Mid-task questions** — if a worker needs my input mid-task, it first checkpoints its progress, then raises the question in Slack, rather than blocking execution.

## Conventions

- **Branching** — nothing is committed directly to `main`. All work happens on branches.
- **Review** — changes are merged via pull requests, which I review from the GitHub mobile app.
- **Deploys** — pushes to `main` trigger a GitHub Actions workflow that SSHes into the VPS, pulls the latest code, and restarts the worker.
- **Secrets** — never committed here and never placed in Notion or Slack. Agents reference secrets by name only; values are injected into the process environment at runtime.

## Running the orchestrator

`orchestrator.py` is the poll loop. Standard library only — no pip install, no venv needed.

```bash
python3 orchestrator.py          # runs until stopped
python3 test_orchestrator.py     # claim/lease self-check, no network
```

### Configuration

All config is environment variables. No secret is ever read from a spec or written into one.

| Variable | Required | Default | What it does |
|---|---|---|---|
| `NOTION_TOKEN` | yes | — | Internal integration secret. The integration must also be added to the Specs database via ••• → Connections, or the API returns 404. |
| `NOTION_DATABASE_ID` | yes | — | The Specs database id. |
| `WORKER_CMD_BACKEND` | one of | — | Shell command that executes a backend spec. |
| `WORKER_CMD_IOS` | these | — | Shell command for iOS specs. Leave unset until the Mac worker exists — unset means those specs are left unclaimed rather than claimed and dropped. |
| `SLACK_BOT_TOKEN` | no | — | `xoxb-` bot token, invited to the channel. Status posting is skipped if absent. |
| `SLACK_CHANNEL` | no | — | Channel id or name — `C0BRL28RM6K` or `my-agents`. A name is resolved to an id once at startup, because `conversations.replies` requires an id even though `chat.postMessage` accepts a name. Needs the `channels:read` scope to resolve a name; pass the id to skip that. |
| `DB_PATH` | no | `orchestrator.db` | SQLite task table. |
| `POLL_SECONDS` | no | `60` | Idle poll interval. |
| `LEASE_SECONDS` | no | `1800` | How long a claim is valid, and the worker timeout. |
| `MAX_ATTEMPTS` | no | `3` | Attempts before a spec is marked `failed`. |

The worker command receives `SPEC_URL`, `SPEC_NAME`, `SPEC_REPO`, and `SPEC_PROJECT` in its
environment. Exit 0 means done; any other exit is an attempt that will be retried until the
limit is reached.

### Claiming and leases

A spec is claimed before it is dispatched, so a slow status write or a restart mid-dispatch
cannot hand the same spec to two workers. Every claim carries an owner, an expiry, and an
attempt count. A worker that dies stops renewing its lease; the orchestrator reclaims the
spec, increments attempts, and re-dispatches. Only a spec that exhausts `MAX_ATTEMPTS` is
marked `failed`. Nothing sits at `in progress` forever.

### Approvals

The state machine does not care who carries a question. It needs two functions:

```python
notify(task, question) -> handle
check_reply(handle) -> reply | None
```

They ship implemented against a Slack thread, because that works with only the bot token.
The intended successor is the iOS app — `POST /approvals` triggering APNs, and
`GET /approvals/{id}` returning the answer — which is a better fit because a reply can be a
full free-text prompt or a revised spec, not just yes/no. Swapping those two functions moves
nothing else.

### Secrets

Secrets live in Infisical (project `my-agents`, env `production`) and are injected into the
process environment at start. Nothing is read from a spec, and no value is committed here.

```bash
infisical run --projectId <id> --env=production -- python3 orchestrator.py
```

`NOTION_DATABASE_ID` and `SLACK_CHANNEL` are configuration, not secrets — set them directly
in the systemd unit rather than storing them in Infisical:

```
NOTION_DATABASE_ID=fbc7943111a6416384f9ad95a447b0df   # the Specs database
SLACK_CHANNEL=C0BRL28RM6K                             # #my-agents
```

The Notion integration should be shared with the **root page** `3c4ac87aab4481b099dcd02c3a04d749`
rather than with the Specs database directly. Access cascades to every child, so the whole page
tree becomes readable in one step and new spec pages need no further sharing.

`CLAUDE_CODE_OAUTH_TOKEN` is the *worker's* credential for headless Agent SDK sessions, not
the orchestrator's. `SLACK_APP_TOKEN` (`xapp-`) is only needed for Socket Mode, which was
ruled out in favour of the iOS app for approvals — it is unused by this code.

# Summary

`my-agents` is a persistent orchestrator system for coordinating automated coding work across a VPS and a local Mac.

## Components

- **Talking agent** — a normal Claude chat session. Discusses architecture/requirements, then writes a self-contained task spec as markdown (`orch-<topic>-<date>.md`) to `docs/architecture/`, on a `docs/orch-spec` branch, opened as a PR.
- **Orchestrator** — runs persistently on the VPS. Polls GitHub for new/updated specs and records each as a task in a SQLite table (`task_id, spec_path, status, branch, last_artifact_path`).
- **VPS worker** — handles general backend tasks. Spawns a headless Claude Agent SDK session per task, commits results to a branch, and opens a PR.
- **Mac worker** — handles iOS/Xcode tasks only, since Xcode requires macOS. Same poll → execute → commit → PR pattern, using Xcode's MCP server for structured build/test/simulator access instead of raw `xcodebuild`.

## Task Lifecycle

1. **Spec generation** — chat agent writes a spec and commits it.
2. **Task ingestion** — orchestrator polls GitHub and parses new specs.
3. **Task queuing** — task recorded in SQLite with status, type, and assigned worker.
4. **Dispatch** — routed to VPS or Mac worker based on task type.
5. **Execution & checkpointing** — worker executes and commits results. If it needs input mid-task, it checkpoints (commits branch state + writes `STATUS.md`), then raises a question via the iOS app rather than blocking. The orchestrator moves on to other work in the meantime.
6. **Resuming after input** — a **fresh** worker session picks up the answer, briefed with the original task + prior `STATUS.md`/branch + the reply. Sessions are disposable; the git state is the memory.
7. **Review & merge** — all work happens on branches, never directly to `main`; PRs are reviewed and merged from the GitHub mobile app.

## Other Conventions

- Generated docs are prefixed `orch-`.
- Machine-specific behavior (e.g. Mac vs. VPS) is driven by a single hook script checked into the repo, branching on an `AGENT_ENV` variable, rather than divergent per-machine configs.

## Explicitly Deferred

- **LangGraph** — not needed unless a workflow requires a fixed, auditable, never-reorderable sequence.
- **Managed Agents API** — an alternative cloud-hosted coordinator/worker option; not used because workers need local filesystem/Xcode access.
- **OpenSpec** — skipped; the underlying "specs as source of truth" practice is kept without the framework, since this is a solo project.
- **Slack-based dashboard** — skipped in favor of the iOS app + a small Python gateway service, to support free-text replies rather than button-only approvals.

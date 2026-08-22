# Orchestrator Setup Steps

A working plan for a multi-agent build system: talking agent → spec file →
worker agent(s) on VPS/Mac → human-in-the-loop approval via mobile.

Prefix convention for anything generated from these conversations: `orch-`

---

## 0. Prerequisites

- [ ] Create the `my-agents` repo on GitHub (manual — no GitHub connector
      available in this session to automate it)
- [ ] Inside it, create `docs/architecture/` — this is where spec files land
- [ ] Decide the branch convention: specs and worker output go on branches
      (e.g. `docs/orch-spec`, `worker/<task-id>`), never straight to `main`.
      You review and merge PRs from the GitHub mobile app.

---

## 1. Talking Agent (this conversation, on desktop)

No infrastructure to build. This is Claude in a normal chat.

- Role: discuss architecture/requirements with you, then write the result
  to a spec file.
- Naming: `orch-<topic>-<date>.md`, e.g. `orch-architecture-2026-08-22.md`
- Destination: `docs/architecture/` in `my-agents`, on a `docs/orch-spec`
  branch, opened as a PR for you to review on your phone.
- Each spec must be **self-contained** — a cold worker with no chat history
  should be able to read it and act, including:
  - which target repo/project it applies to (specs can span multiple
    projects from one `my-agents` repo)
  - acceptance criteria
  - any constraints/contracts (e.g. shared API shape between iOS + backend)

---

## 2. VPS Worker (backend / non-Mac tasks)

- [ ] Install Claude Agent SDK on the VPS
- [ ] Authenticate via OAuth token tied to your subscription (not metered
      API key) — supports headless use
- [ ] Build a simple poll loop:
  ```
  while True:
      check docs/architecture/ (or a task table) for new/updated specs
      for each new spec:
          spawn a headless Agent SDK session with the spec as the brief
          on completion: commit to a branch, open PR, update task status
  ```
- [ ] Persist task state in SQLite (or similar) — not in the SDK session.
      Minimum fields: `task_id, spec_path, status, branch, last_artifact_path`
- [ ] Handle the SDK's own usage-limit resets (retry/backoff, don't crash)

---

## 3. Mac Worker (iOS-specific tasks only)

Anything touching Xcode/simulators must run here — the VPS cannot build iOS.

- [ ] Same poll-loop pattern as the VPS worker, filtered to specs tagged
      for the iOS project
- [ ] Wire in Xcode's MCP server so the worker uses structured build/test/
      simulator tools instead of raw `xcodebuild` via shell
- [ ] Same commit → PR → status-update pattern as the VPS worker

---

## 4. Human-in-the-Loop (approval / free-text input)

Chosen approach: your existing iOS app + a small Python gateway service —
no Telegram, no third-party notification service.

- [ ] Python service exposes two things:
  1. An endpoint the orchestrator/worker calls to push a pending question
     (not just yes/no — can carry a full free-text prompt or revised spec)
  2. A way to receive your reply and hand it back to the waiting task
- [ ] Your iOS app receives the push and lets you respond in whatever form
      the question needs (approve/reject, or open text)

### Parking mechanism (don't block the whole orchestrator on one answer)

- When a worker hits a gate, it must **checkpoint first**:
  - commit its current branch state
  - write a short `STATUS.md`: what's done, what's pending, what the
    question is
- Only then does it raise the question and mark itself `awaiting_human`
  in the task table.
- The orchestrator moves on to other dispatchable work — it does not block.
- When your reply arrives, don't resume the old session. Spawn a **fresh**
  worker session with a self-contained brief: original task + prior
  worker's `STATUS.md`/branch + your answer. The files are the memory;
  the conversation was always disposable.

---

## 5. Machine-Specific Hooks

- [ ] One hook script, checked into the repo, branching on an environment
      variable (e.g. `AGENT_ENV=mac` vs `AGENT_ENV=vps`)
- [ ] Example: a post-edit hook runs an Xcode build only when
      `AGENT_ENV=mac`; on the VPS the same hook runs a Python linter instead
- [ ] Keeps one config in version control instead of divergent per-machine
      setups

---

## 6. Explicitly Deferred / Rejected

- **LangGraph**: not needed to start. Revisit only if a workflow needs a
  fixed, never-reorderable sequence (spec → implement → test → approve →
  merge) with auditable path traces, or long-gap resume-exactly-here
  behavior. Otherwise the Agent SDK loop + external state (SQLite/git) is
  simpler and sufficient.
- **Managed Agents API (cloud coordinator)**: an alternative to the VPS
  poll-loop worth knowing about — Anthropic runs the coordinator/worker
  agents on their own sandboxed cloud VMs rather than your infrastructure.
  Not chosen here because workers need local filesystem/Xcode access; kept
  as an option for pure backend/research tasks that don't need local
  machine access.
- **OpenSpec**: skipped. Its value is multi-person coordination and change
  review; for a solo project the ceremony isn't worth it. The underlying
  practice — writing specs as the source of truth — is kept; just without
  the framework.
- **Slack-based dashboard**: skipped in favor of the iOS app + Python
  gateway, since you already have the app and want free-text replies, not
  just button approvals.

---

## Build Order (start here, expand later)

1. Create `my-agents` repo + `docs/architecture/` + branch convention
2. Talking agent (already working) writes specs there
3. VPS worker: poll loop + headless Agent SDK + SQLite task table
4. Only once the above is annoying without it: add the Python gateway +
   parking mechanism for human-in-the-loop
5. Add the Mac worker + Xcode MCP when an iOS task actually comes up
6. Add machine-specific hooks once you have both workers running

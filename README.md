# my-agents

A persistent orchestrator agent system for coordinating automated coding work across a VPS and a local Mac.

## Overview

An **orchestrator** runs persistently on my VPS and coordinates two types of **worker agents**:

- **Backend workers** — run on the VPS or a cloud VM, for general backend tasks.
- **Mac workers** — run on my Mac, dedicated to iOS builds, since Xcode is required to compile those and only runs on macOS.

## How it works

1. **Task creation** — I write task specifications as markdown files during chat sessions.
2. **Commit** — those specs are committed to this repository under `docs/architecture`.
3. **Polling** — the orchestrator polls GitHub for new tasks.
4. **Dispatch** — each task is routed to the appropriate worker type based on the task's declared type (backend vs. iOS/Xcode).
5. **Execution** — workers execute their assigned tasks and commit results back to git.
6. **Mid-task questions** — if a worker needs my input mid-task, it first checkpoints its progress, then raises a question through my iOS app, rather than blocking execution.

## Conventions

- **Generated docs** are prefixed `orch-`.
- **Architecture specs** live under [`docs/architecture`](docs/architecture).
- **Branching** — nothing is committed directly to `main`. All work happens on branches.
- **Review** — changes are merged via pull requests, which I review from the GitHub mobile app.

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

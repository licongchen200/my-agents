# Task Lifecycle

The goal of this system is autonomous task orchestration: a conversational chat agent and background worker agents collaborate on tasks without needing constant supervision.

## Step 1 — Spec Generation

During chat sessions, the chat agent generates task specifications as markdown files based on what was discussed, and commits them to this GitHub repository.

## Step 2 — Task Ingestion

The orchestrator, running persistently on the VPS, polls GitHub for new task files and parses them.

## Step 3 — Task Queuing

Each parsed task is recorded in a SQLite database, tracking:

- Status
- Task type
- Assigned worker

## Step 4 — Dispatch

Based on task type, the orchestrator routes the task to the right worker:

- **Backend tasks** → VPS or cloud worker
- **iOS build tasks** → Mac worker (Xcode is required and only runs on macOS)

## Step 5 — Execution and Checkpointing

Workers execute their tasks and commit results back to git.

If a worker needs user input mid-task, it:

1. Checkpoints its progress first.
2. Raises a question through the iOS app, rather than blocking.

## Step 6 — Review and Merge

All changes happen on branches, never directly to `main`. Pull requests are reviewed and merged from the GitHub mobile app.

"""Orchestrator: polls Notion for specs, claims one with a lease, dispatches it to a worker.

State lives in SQLite, not in the worker session. A worker that dies stops renewing its
lease; the orchestrator reclaims the spec, increments its attempt count, and re-dispatches.
Only a spec that exhausts MAX_ATTEMPTS is marked failed.

Config is all environment variables. No secret is ever read from a spec or written to one.
"""

import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "")
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL = os.environ.get("SLACK_CHANNEL", "")

DB_PATH = os.environ.get("DB_PATH", "orchestrator.db")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "60"))
LEASE_SECONDS = int(os.environ.get("LEASE_SECONDS", "1800"))
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "3"))
OWNER = os.environ.get("OWNER", f"orchestrator-{os.getpid()}")

# Task type -> shell command template. A type with no command here is left unclaimed,
# so ios specs sit untouched until the Mac worker exists rather than being claimed and lost.
WORKERS = {t: os.environ[k] for t, k in (("backend", "WORKER_CMD_BACKEND"),
                                         ("ios", "WORKER_CMD_IOS")) if k in os.environ}

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    spec_url         TEXT PRIMARY KEY,
    spec_id          INTEGER,
    name             TEXT,
    task_type        TEXT,
    project          TEXT,
    repo             TEXT,
    status           TEXT NOT NULL,
    owner            TEXT,
    lease_expires_at TEXT,
    attempts         INTEGER NOT NULL DEFAULT 0,
    approval_handle  TEXT,
    last_error       TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status, lease_expires_at);
"""


def now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.isoformat()


def connect(path=DB_PATH):
    db = sqlite3.connect(path, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    return db


# --- HTTP -------------------------------------------------------------------

def http(method, url, headers=None, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {url} -> {e.code}: {e.read()[:400].decode(errors='replace')}") from None


def notion(method, path, body=None):
    return http(method, f"https://api.notion.com/v1{path}",
                {"Authorization": f"Bearer {NOTION_TOKEN}", "Notion-Version": "2022-06-28"}, body)


def slack(api, body):
    r = http("POST", f"https://slack.com/api/{api}",
             {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}, body)
    if not r.get("ok"):
        raise RuntimeError(f"slack {api} failed: {r.get('error')}")
    return r


def post_status(text):
    """Status is one-way and best-effort. A Slack outage must not stall the queue."""
    if not (SLACK_BOT_TOKEN and SLACK_CHANNEL):
        return
    try:
        slack("chat.postMessage", {"channel": SLACK_CHANNEL, "text": text})
    except Exception as e:
        print(f"slack post failed, continuing: {e}", file=sys.stderr)


# --- Approval seam ----------------------------------------------------------
# The state machine does not care who carries the question. Swapping these two
# functions to the iOS app (POST /approvals + APNs, GET /approvals/{id}) moves
# nothing else. Slack is here because it works with only the bot token.
# ponytail: Slack thread implementation; swap to the iOS backend once its
# endpoint shape is confirmed (is it on this VPS, does it store a pending question).

def notify(task, question):
    r = slack("chat.postMessage", {
        "channel": SLACK_CHANNEL,
        "text": f"*{task['name']}* (SPEC-{task['spec_id']}) needs you:\n{question}\n_Reply in this thread._",
    })
    return r["ts"]


def check_reply(handle):
    r = slack("conversations.replies", {"channel": SLACK_CHANNEL, "ts": handle, "limit": 20})
    for m in r.get("messages", [])[1:]:
        if not m.get("bot_id"):
            return m.get("text")
    return None


# --- Notion sync ------------------------------------------------------------

def plain(prop):
    if not prop:
        return None
    kind = prop.get("type")
    if kind == "select":
        return (prop.get("select") or {}).get("name")
    if kind == "url":
        return prop.get("url")
    if kind == "unique_id":
        return (prop.get("unique_id") or {}).get("number")
    if kind in ("title", "rich_text"):
        return "".join(t.get("plain_text", "") for t in prop.get(kind, [])) or None
    return None


def pull_specs(db):
    """Copy 'not started' specs into the task table. Idempotent; existing rows are left alone."""
    res = notion("POST", f"/databases/{NOTION_DATABASE_ID}/query",
                 {"filter": {"property": "Status", "select": {"equals": "not started"}}})
    seen = 0
    for page in res.get("results", []):
        p = page.get("properties", {})
        db.execute(
            """INSERT INTO tasks (spec_url, spec_id, name, task_type, project, repo,
                                  status, attempts, created_at, updated_at)
               VALUES (?,?,?,?,?,?, 'not started', 0, ?, ?)
               ON CONFLICT(spec_url) DO NOTHING""",
            (page["url"], plain(p.get("Spec ID")), plain(p.get("Name")), plain(p.get("Task type")),
             plain(p.get("Project")), plain(p.get("Repo")), iso(now()), iso(now())))
        seen += 1
    return seen


def push_status(spec_url, status):
    page_id = spec_url.rstrip("/").split("/")[-1].split("-")[-1]
    notion("PATCH", f"/pages/{page_id}", {"properties": {"Status": {"select": {"name": status}}}})


# --- Claiming ---------------------------------------------------------------

def claim(db, owner=OWNER, task_types=None):
    """Atomically take one dispatchable spec. Returns the row, or None.

    Dispatchable means never started, or in progress with an expired lease (the worker
    died). Reclaiming counts as another attempt. A spec at the attempt limit is failed
    instead of handed out again.
    """
    types = list(task_types if task_types is not None else WORKERS.keys())
    if not types:
        return None
    marks = ",".join("?" * len(types))
    t = now()
    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute(
            f"""SELECT * FROM tasks
                 WHERE task_type IN ({marks})
                   AND (status = 'not started'
                        OR (status = 'in progress' AND lease_expires_at < ?))
                 ORDER BY attempts, created_at LIMIT 1""",
            (*types, iso(t))).fetchone()
        if row is None:
            db.execute("COMMIT")
            return None
        attempts = row["attempts"] + 1
        if attempts > MAX_ATTEMPTS:
            db.execute("UPDATE tasks SET status='failed', owner=NULL, lease_expires_at=NULL,"
                       " last_error='attempt limit exhausted', updated_at=? WHERE spec_url=?",
                       (iso(t), row["spec_url"]))
            db.execute("COMMIT")
            return claim(db, owner, types)  # skip past it, look for real work
        db.execute("UPDATE tasks SET status='in progress', owner=?, lease_expires_at=?,"
                   " attempts=?, updated_at=? WHERE spec_url=?",
                   (owner, iso(t + timedelta(seconds=LEASE_SECONDS)), attempts, iso(t), row["spec_url"]))
        db.execute("COMMIT")
        return db.execute("SELECT * FROM tasks WHERE spec_url=?", (row["spec_url"],)).fetchone()
    except Exception:
        db.execute("ROLLBACK")
        raise


def finish(db, spec_url, status, error=None):
    db.execute("UPDATE tasks SET status=?, owner=NULL, lease_expires_at=NULL,"
               " last_error=?, updated_at=? WHERE spec_url=?",
               (status, error, iso(now()), spec_url))


# --- Dispatch ---------------------------------------------------------------

def dispatch(db, task):
    cmd = WORKERS[task["task_type"]]
    env = {**os.environ, "SPEC_URL": task["spec_url"], "SPEC_NAME": task["name"] or "",
           "SPEC_REPO": task["repo"] or "", "SPEC_PROJECT": task["project"] or ""}
    post_status(f":arrow_forward: SPEC-{task['spec_id']} {task['name']} — started (attempt {task['attempts']})")
    try:
        r = subprocess.run(cmd, shell=True, env=env, capture_output=True, text=True,
                           timeout=LEASE_SECONDS)
    except subprocess.TimeoutExpired:
        finish(db, task["spec_url"], "in progress", "worker timed out; lease will expire")
        post_status(f":hourglass: SPEC-{task['spec_id']} timed out, will be reclaimed")
        return
    if r.returncode == 0:
        finish(db, task["spec_url"], "done")
        push_status(task["spec_url"], "done")
        post_status(f":white_check_mark: SPEC-{task['spec_id']} {task['name']} — done")
    else:
        # Leave it in progress with a live lease expiry so the retry path owns the decision.
        db.execute("UPDATE tasks SET lease_expires_at=?, last_error=?, updated_at=? WHERE spec_url=?",
                   (iso(now()), (r.stderr or "")[-500:], iso(now()), task["spec_url"]))
        post_status(f":x: SPEC-{task['spec_id']} {task['name']} — failed attempt {task['attempts']}")


def tick(db):
    pull_specs(db)
    task = claim(db)
    if task is None:
        return False
    push_status(task["spec_url"], "in progress")
    dispatch(db, task)
    return True


def main():
    missing = [k for k, v in (("NOTION_TOKEN", NOTION_TOKEN),
                              ("NOTION_DATABASE_ID", NOTION_DATABASE_ID)) if not v]
    if missing:
        sys.exit(f"missing required env: {', '.join(missing)}")
    if not WORKERS:
        sys.exit("no worker commands configured (set WORKER_CMD_BACKEND and/or WORKER_CMD_IOS)")
    db = connect()
    print(f"orchestrator {OWNER} up; workers={list(WORKERS)}; poll={POLL_SECONDS}s")
    while True:
        try:
            if not tick(db):
                time.sleep(POLL_SECONDS)
        except Exception as e:
            print(f"tick failed: {e}", file=sys.stderr)
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()

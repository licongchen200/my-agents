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
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "")
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL = os.environ.get("SLACK_CHANNEL", "")
APPROVALS_DATABASE_ID = os.environ.get("APPROVALS_DATABASE_ID", "")
NOTIFY_USER_ID = os.environ.get("NOTIFY_USER_ID", "")  # Notion user id to @mention

DB_PATH = os.environ.get("DB_PATH", "orchestrator.db")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "60"))
LEASE_SECONDS = int(os.environ.get("LEASE_SECONDS", "1800"))
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "2"))
EXIT_SPLIT = 2  # worker decomposed the spec into children; not a failure, never retried
OWNER = os.environ.get("OWNER", f"orchestrator-{os.getpid()}")

# Task type -> shell command template. A type with no command here is left unclaimed,
# so ios specs sit untouched until the Mac worker exists rather than being claimed and lost.
# Task type -> how the work runs. A type with no entry here is left unclaimed, so specs
# for a worker that does not exist yet sit untouched rather than being claimed and dropped.
WORKERS = {t: os.environ[k] for t, k in (("infra", "WORKER_CMD_INFRA"),
                                         ("ios", "WORKER_CMD_IOS")) if k in os.environ}

# backend specs run on an ephemeral GitHub runner instead of this box. A cloud runner
# cannot touch the VPS, which is why infra stays local rather than moving too.
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
CLOUD_WORKFLOW = os.environ.get("CLOUD_WORKFLOW", "spec-worker.yml")
CLOUD_REPO = os.environ.get("CLOUD_REPO", "")
CLOUD_REF = os.environ.get("CLOUD_REF", "main")
if GITHUB_TOKEN and CLOUD_REPO:
    WORKERS["backend"] = "cloud"

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
    """Write methods take a JSON body."""
    r = http("POST", f"https://slack.com/api/{api}",
             {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}, body)
    if not r.get("ok"):
        raise RuntimeError(f"slack {api} failed: {r.get('error')}")
    return r


def slack_get(api, params):
    """Read methods reject a JSON body — they need GET with query params. Posting a
    JSON body to conversations.replies returns invalid_arguments, not a useful error."""
    url = f"https://slack.com/api/{api}?" + urllib.parse.urlencode(params)
    r = http("GET", url, {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"})
    if not r.get("ok"):
        raise RuntimeError(f"slack {api} failed: {r.get('error')}")
    return r


def resolve_channel(name_or_id):
    """Accept a channel id or a name. conversations.replies needs an id, while
    chat.postMessage tolerates a name — so a name-only config posts status fine and
    then silently fails to ever read an approval reply. Resolve once at startup."""
    if not name_or_id or name_or_id.startswith(("C", "G", "D")):
        return name_or_id
    wanted = name_or_id.lstrip("#")
    cursor = None
    while True:
        r = slack_get("conversations.list", {"limit": 200, "exclude_archived": "true",
                                             "types": "public_channel,private_channel",
                                             **({"cursor": cursor} if cursor else {})})
        for c in r.get("channels", []):
            if c.get("name") == wanted:
                return c["id"]
        cursor = (r.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            raise RuntimeError(f"slack channel {name_or_id!r} not found; is the bot invited?")


def post_status(text):
    """Status is one-way and best-effort. A Slack outage must not stall the queue."""
    if not (SLACK_BOT_TOKEN and SLACK_CHANNEL):
        return
    try:
        slack("chat.postMessage", {"channel": SLACK_CHANNEL, "text": text})
    except Exception as e:
        print(f"slack post failed, continuing: {e}", file=sys.stderr)


# --- Approval seam ----------------------------------------------------------
# Questions live in the Approvals and Requests database in Notion, where an answer can
# be free text or a completely rewritten brief. Slack only carries the ping, because
# Notion notifies reliably on @mention and unreliably on a new row appearing.
# The state machine does not care: it is still just notify() and check_reply().

def notify(task, question):
    """Create an approval row and ping Slack. Returns the row's page id as the handle."""
    if not APPROVALS_DATABASE_ID:
        raise RuntimeError("APPROVALS_DATABASE_ID is not set; cannot ask for approval")
    mention = ([{"type": "mention", "mention": {"type": "user", "user": {"id": NOTIFY_USER_ID}}},
                {"type": "text", "text": {"content": " — a worker is parked on this."}}]
               if NOTIFY_USER_ID else
               [{"type": "text", "text": {"content": "A worker is parked on this."}}])
    page = notion("POST", "/pages", {
        "parent": {"database_id": APPROVALS_DATABASE_ID},
        "properties": {
            "Question": {"title": [{"text": {"content": question[:200]}}]},
            "Status": {"select": {"name": "waiting for you"}},
            "Spec": {"url": task["spec_url"]},
            "Spec name": {"rich_text": [{"text": {"content": task["name"] or ""}}]},
        },
        "children": [
            {"object": "block", "type": "paragraph",
             "paragraph": {"rich_text": mention}},
            {"object": "block", "type": "paragraph",
             "paragraph": {"rich_text": [{"text": {"content": question}}]}},
            {"object": "block", "type": "callout",
             "callout": {"icon": {"emoji": "✍️"},
                         "rich_text": [{"text": {"content":
                            "Write your reply in the Answer property, then set Status to "
                            "answered. Free text is fine, including a fully rewritten brief. "
                            "Nothing is read until Status is answered, so a half typed "
                            "answer is never picked up."}}]}},
        ],
    })
    post_status(f":raising_hand: *{task['name']}* needs you — {page['url']}")
    return page["id"]


def check_reply(handle):
    """Return the answer once the row is marked answered, else None."""
    page = notion("GET", f"/pages/{handle}")
    props = page.get("properties", {})
    if plain(props.get("Status")) != "answered":
        return None
    return plain(props.get("Answer")) or ""


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

def spec_body(spec_url):
    """Flatten the spec page so it can be passed to a cloud runner that has no Notion token."""
    page_id = spec_url.rstrip("/").split("/")[-1].split("?")[0].split("-")[-1]
    out = []
    res = notion("GET", f"/blocks/{page_id}/children?page_size=100")
    for b in res.get("results", []):
        t = b.get("type")
        rt = (b.get(t) or {}).get("rich_text")
        if rt:
            out.append("".join(i.get("plain_text", "") for i in rt))
    return "\n".join(out)


def dispatch(db, task):
    if WORKERS.get(task["task_type"]) == "cloud":
        return dispatch_cloud(db, task, spec_body(task["spec_url"]))
    cmd = WORKERS[task["task_type"]]
    env = {**os.environ, "SPEC_URL": task["spec_url"], "SPEC_NAME": task["name"] or "",
           "SPEC_REPO": task["repo"] or "", "SPEC_PROJECT": task["project"] or ""}
    post_status(f":arrow_forward: SPEC-{task['spec_id']} {task['name']} — started (attempt {task['attempts']})")
    try:
        r = subprocess.run(cmd, shell=True, env=env, capture_output=True, text=True,
                           timeout=LEASE_SECONDS)
    except subprocess.TimeoutExpired:
        # Deterministic, not transient: the same spec will time out again on the same
        # budget. Retrying burns another full session for the same result, so surface it
        # instead. Split the spec or raise LEASE_SECONDS.
        finish(db, task["spec_url"], "failed",
               f"worker exceeded {LEASE_SECONDS}s; not retried (deterministic)")
        push_status(task["spec_url"], "failed")
        post_status(f":hourglass: SPEC-{task['spec_id']} {task['name']} — exceeded "
                    f"{LEASE_SECONDS // 60}m and was not retried. Split it, or raise the lease.")
        return
    if r.returncode == 0:
        finish(db, task["spec_url"], "done")
        push_status(task["spec_url"], "done")
        post_status(f":white_check_mark: SPEC-{task['spec_id']} {task['name']} — done")
    elif r.returncode == EXIT_SPLIT:
        # Too big for one session. The worker already created the children, which are
        # queued as ordinary specs. Retrying the parent would only split it again.
        finish(db, task["spec_url"], "split")
        push_status(task["spec_url"], "split")
        post_status(f":scissors: SPEC-{task['spec_id']} {task['name']} — too big for one "
                    f"session, split into child specs")
    else:
        # Leave it in progress with a live lease expiry so the retry path owns the decision.
        db.execute("UPDATE tasks SET lease_expires_at=?, last_error=?, updated_at=? WHERE spec_url=?",
                   (iso(now()), (r.stderr or "")[-500:], iso(now()), task["spec_url"]))
        post_status(f":x: SPEC-{task['spec_id']} {task['name']} — failed attempt {task['attempts']}")



# --- Cloud dispatch ----------------------------------------------------------

def github(path, method="GET", body=None):
    return http(method, f"https://api.github.com{path}",
                {"Authorization": f"Bearer {GITHUB_TOKEN}",
                 "Accept": "application/vnd.github+json"}, body)


def dispatch_cloud(db, task, body_text):
    """Fire the workflow, then poll until the run finishes or the lease runs out.

    workflow_dispatch returns no run id, so the run is identified by matching the most
    recent run of this workflow created after we fired it.
    """
    started = now()
    github(f"/repos/{CLOUD_REPO}/actions/workflows/{CLOUD_WORKFLOW}/dispatches", "POST",
           {"ref": CLOUD_REF, "inputs": {
               "spec_url": task["spec_url"],
               "spec_name": (task["name"] or "")[:200],
               "spec_body": body_text[:60000],
               "spec_project": task["project"] or "",
           }})
    post_status(f":cloud: SPEC-{task['spec_id']} {task['name']} — dispatched to GitHub Actions")

    run = None
    deadline = started + timedelta(seconds=LEASE_SECONDS)
    while now() < deadline:
        time.sleep(15)
        runs = github(f"/repos/{CLOUD_REPO}/actions/workflows/{CLOUD_WORKFLOW}/runs"
                      f"?event=workflow_dispatch&per_page=10").get("workflow_runs", [])
        fresh = [r for r in runs if r["created_at"] >= iso(started)[:19]]
        if not fresh:
            continue
        run = fresh[0]
        if run["status"] == "completed":
            break
    if run is None:
        finish(db, task["spec_url"], "failed", "cloud run never appeared")
        push_status(task["spec_url"], "failed")
        post_status(f":x: SPEC-{task['spec_id']} — dispatched but no run appeared")
        return
    if run["status"] != "completed":
        finish(db, task["spec_url"], "failed",
               f"cloud run exceeded {LEASE_SECONDS}s; not retried (deterministic)")
        push_status(task["spec_url"], "failed")
        post_status(f":hourglass: SPEC-{task['spec_id']} — cloud run exceeded "
                    f"{LEASE_SECONDS // 60}m: {run['html_url']}")
        return
    if run["conclusion"] == "success":
        finish(db, task["spec_url"], "done")
        push_status(task["spec_url"], "done")
        post_status(f":white_check_mark: SPEC-{task['spec_id']} {task['name']} — done "
                    f"({run['html_url']})")
    else:
        db.execute("UPDATE tasks SET lease_expires_at=?, last_error=?, updated_at=? "
                   "WHERE spec_url=?",
                   (iso(now()), f"cloud run {run['conclusion']}: {run['html_url']}",
                    iso(now()), task["spec_url"]))
        post_status(f":x: SPEC-{task['spec_id']} {task['name']} — cloud run "
                    f"{run['conclusion']} (attempt {task['attempts']}): {run['html_url']}")


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
        sys.exit("no workers configured (set WORKER_CMD_INFRA, WORKER_CMD_IOS, or GITHUB_TOKEN + CLOUD_REPO for cloud backend specs)")
    global SLACK_CHANNEL
    if SLACK_BOT_TOKEN and SLACK_CHANNEL:
        SLACK_CHANNEL = resolve_channel(SLACK_CHANNEL)
    db = connect()
    print(f"orchestrator {OWNER} up; workers={list(WORKERS)}; "
          f"channel={SLACK_CHANNEL or 'none'}; poll={POLL_SECONDS}s")
    while True:
        try:
            if not tick(db):
                time.sleep(POLL_SECONDS)
        except Exception as e:
            print(f"tick failed: {e}", file=sys.stderr)
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()

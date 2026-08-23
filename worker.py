"""Worker: turn one Notion spec into a pull request.

Invoked by the orchestrator with the spec's details in the environment. Fetches the
spec body from Notion, checks out the target repo, hands the whole thing to a headless
Claude Code session as a self-contained brief, then commits the result to a branch and
opens a PR. Exit 0 means done; any other exit is an attempt the orchestrator will retry.

The conversation is disposable — the branch and the PR are the memory.
"""

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]

SPEC_URL = os.environ["SPEC_URL"]
SPEC_NAME = os.environ.get("SPEC_NAME", "untitled spec")
SPEC_REPO = os.environ.get("SPEC_REPO", "")
SPEC_PROJECT = os.environ.get("SPEC_PROJECT", "")

WORK_ROOT = os.environ.get("WORK_ROOT", "/root/apps/orchestrator/work")
SPECS_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "")
# A session is sized for one focused change. Anything the planner judges bigger is split
# into child specs rather than attempted in one shot and timing out at the lease.
SESSION_MINUTES = int(os.environ.get("SESSION_MINUTES", "40"))
EXIT_SPLIT = 2  # orchestrator maps this to status=split and does NOT retry
CLAUDE = os.environ.get("CLAUDE_BIN", "/root/.local/bin/claude")
# The user chose to run headless sessions without permission prompts.
# ponytail: unrestricted Bash as whoever runs this; run as a dedicated non-root
# user to cap blast radius on a box that also serves production traffic.
CLAUDE_ARGS = os.environ.get(
    "CLAUDE_ARGS", "--dangerously-skip-permissions --output-format text").split()


def run(cmd, cwd=None, check=True, capture=True):
    r = subprocess.run(cmd, cwd=cwd, capture_output=capture, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:3])}… failed ({r.returncode}): "
                           f"{(r.stderr or r.stdout or '')[-500:]}")
    return r


# --- Notion: spec body -> brief ---------------------------------------------

def notion(path, method="GET", body=None):
    req = urllib.request.Request(f"https://api.notion.com/v1{path}",
                                 data=json.dumps(body).encode() if body else None,
                                 method=method)
    req.add_header("Authorization", f"Bearer {NOTION_TOKEN}")
    req.add_header("Notion-Version", "2022-06-28")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def rich(items):
    return "".join(i.get("plain_text", "") for i in items or [])


PREFIX = {"heading_1": "# ", "heading_2": "## ", "heading_3": "### ",
          "bulleted_list_item": "- ", "numbered_list_item": "1. ", "quote": "> "}


def blocks_to_text(block_id, depth=0):
    """Flatten a Notion page into markdown. One nested level is plenty for a spec."""
    out = []
    cursor = None
    while True:
        q = f"?page_size=100" + (f"&start_cursor={cursor}" if cursor else "")
        res = notion(f"/blocks/{block_id}/children{q}")
        for b in res.get("results", []):
            t = b.get("type")
            body = b.get(t, {})
            pad = "  " * depth
            if t == "divider":
                out.append("---")
            elif t == "code":
                out.append(f"```{body.get('language','')}\n{rich(body.get('rich_text'))}\n```")
            elif t == "to_do":
                mark = "x" if body.get("checked") else " "
                out.append(f"{pad}- [{mark}] {rich(body.get('rich_text'))}")
            elif "rich_text" in body:
                text = rich(body.get("rich_text"))
                if text:
                    out.append(f"{pad}{PREFIX.get(t, '')}{text}")
            if b.get("has_children") and depth < 2 and t != "code":
                out.append(blocks_to_text(b["id"], depth + 1))
        cursor = res.get("next_cursor")
        if not cursor:
            break
    return "\n".join(x for x in out if x)


def page_id_from_url(url):
    tail = url.rstrip("/").split("/")[-1].split("?")[0]
    return tail.split("-")[-1]


def build_brief(body):
    return f"""You are a worker agent. Implement the specification below, end to end.

Project: {SPEC_PROJECT or 'unspecified'}
Repository: {SPEC_REPO or 'unspecified'}
Spec: {SPEC_NAME}
Source: {SPEC_URL}

You are already inside a fresh checkout of the repository, on a new branch. Make the
changes the spec calls for, directly in this working tree. Do not open a pull request
or push anything — that is handled for you after you finish.

If the spec is ambiguous, choose the smaller interpretation and note the assumption in
your final message. If the spec cannot be done at all, change nothing and say why.

--- SPECIFICATION ---
{body}
--- END SPECIFICATION ---"""



# --- Decomposition ------------------------------------------------------------

PLAN_PROMPT = """Assess the specification below. Do NOT implement anything.

One worker session is a single focused change: roughly under {minutes} minutes of work,
touching a coherent set of files, reviewable as one pull request. Provisioning a machine
end to end is not one session. Adding one file, one endpoint, or one schema is.

Reply with ONLY a JSON object, no prose and no code fence:

{{"decompose": false}}

or

{{"decompose": true, "reason": "<one sentence>", "children": [
  {{"name": "<short imperative spec title>", "brief": "<self-contained brief: goal, tasks, acceptance criteria>"}}
]}}

Split only if genuinely too large. Prefer 2 to 6 children. Each child must stand alone: a
worker with no memory of this spec or its siblings must be able to act on it. Order them so
that run in sequence each makes sense. Do not invent work the spec does not ask for, and do
not split merely because the spec has several checkboxes.

--- SPECIFICATION ---
{body}
--- END SPECIFICATION ---"""


def plan_split(body):
    """Ask whether this is one session's work. Returns a plan dict, or None to proceed."""
    args = [a for a in CLAUDE_ARGS if a not in ("--output-format", "text")]
    r = subprocess.run([CLAUDE, "-p", PLAN_PROMPT.format(minutes=SESSION_MINUTES, body=body),
                        "--output-format", "text", *args], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"[worker] planner failed, proceeding unsplit: {(r.stderr or '')[-300:]}", flush=True)
        return None
    text = (r.stdout or "").strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        print(f"[worker] planner gave no JSON, proceeding unsplit: {text[:200]!r}", flush=True)
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        print("[worker] planner JSON unparseable, proceeding unsplit", flush=True)
        return None


def create_child_specs(plan):
    """Write child specs back into the Specs database. Returns their urls."""
    if not SPECS_DATABASE_ID:
        raise RuntimeError("NOTION_DATABASE_ID is not set; cannot create child specs")
    children = plan.get("children") or []
    if not children:
        return []
    urls = []
    for i, child in enumerate(children, 1):
        page = notion("/pages", "POST", {
            "parent": {"database_id": SPECS_DATABASE_ID},
            "properties": {
                "Name": {"title": [{"text": {"content": str(child["name"])[:200]}}]},
                "Status": {"select": {"name": "not started"}},
                "Task type": {"select": {"name": os.environ.get("SPEC_TASK_TYPE", "backend")}},
                "Project": {"rich_text": [{"text": {"content": SPEC_PROJECT}}]},
                "Repo": {"url": SPEC_REPO or None},
                "Parent": {"url": SPEC_URL},
            },
            "children": [
                {"object": "block", "type": "paragraph", "paragraph": {"rich_text": [
                    {"text": {"content": f"Step {i} of {len(children)}, split out of "
                                         f"{SPEC_NAME}."}}]}},
                {"object": "block", "type": "paragraph", "paragraph": {"rich_text": [
                    {"text": {"content": str(child["brief"])[:1900]}}]}},
            ],
        })
        urls.append(page["url"])
        print(f"[worker]   child {i}: {child['name']} -> {page['url']}", flush=True)
    return urls


# --- GitHub ------------------------------------------------------------------

def repo_slug(url):
    m = re.search(r"github\.com[:/]+([^/]+/[^/.]+)", url or "")
    if not m:
        raise RuntimeError(f"cannot parse a github repo out of {url!r}")
    return m.group(1)


def open_pr(slug, branch, base, title, body):
    req = urllib.request.Request(
        f"https://api.github.com/repos/{slug}/pulls",
        data=json.dumps({"title": title, "head": branch, "base": base, "body": body}).encode(),
        method="POST")
    req.add_header("Authorization", f"Bearer {GITHUB_TOKEN}")
    req.add_header("Accept", "application/vnd.github+json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())["html_url"]
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"pr create failed ({e.code}): {e.read()[:400].decode(errors='replace')}")


# --- main --------------------------------------------------------------------

def slug_for(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "spec"


def main():
    page = page_id_from_url(SPEC_URL)
    body = blocks_to_text(page)
    if not body.strip():
        sys.exit(f"spec {SPEC_URL} has no body to act on")

    plan = plan_split(body)
    if plan and plan.get("decompose"):
        print(f"[worker] too big for one session: {plan.get('reason', '')}", flush=True)
        urls = create_child_specs(plan)
        if urls:
            print(f"[worker] split into {len(urls)} child spec(s)", flush=True)
            sys.exit(EXIT_SPLIT)
        print("[worker] planner said split but produced no children; proceeding", flush=True)

    slug = repo_slug(SPEC_REPO)
    branch = f"worker/{slug_for(SPEC_NAME)}"
    workdir = os.path.join(WORK_ROOT, page)
    os.makedirs(WORK_ROOT, exist_ok=True)
    run(["rm", "-rf", workdir])

    auth_url = f"https://x-access-token:{GITHUB_TOKEN}@github.com/{slug}.git"
    print(f"[worker] cloning {slug} -> {workdir}", flush=True)
    run(["git", "clone", "--depth", "1", auth_url, workdir])
    base = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=workdir).stdout.strip()
    run(["git", "checkout", "-b", branch], cwd=workdir)
    run(["git", "config", "user.name", "orchestrator-worker"], cwd=workdir)
    run(["git", "config", "user.email", "worker@licongchen.org"], cwd=workdir)

    print(f"[worker] running claude on {SPEC_NAME!r}", flush=True)
    r = subprocess.run([CLAUDE, "-p", build_brief(body), *CLAUDE_ARGS],
                       cwd=workdir, capture_output=True, text=True)
    summary = (r.stdout or "").strip()
    print(f"[worker] claude exit={r.returncode}\n{summary[-2000:]}", flush=True)
    if r.returncode != 0:
        sys.exit(f"claude failed: {(r.stderr or '')[-500:]}")

    if not run(["git", "status", "--porcelain"], cwd=workdir).stdout.strip():
        sys.exit("worker made no changes — treating as a failed attempt, not a silent success")

    run(["git", "add", "-A"], cwd=workdir)
    run(["git", "commit", "-m", f"{SPEC_NAME}\n\nImplements {SPEC_URL}\n\n"
                                f"Generated by a headless worker session."], cwd=workdir)
    run(["git", "push", "-u", "origin", branch], cwd=workdir)

    url = open_pr(slug, branch, base, SPEC_NAME,
                  f"Implements [{SPEC_NAME}]({SPEC_URL}).\n\n"
                  f"Project: {SPEC_PROJECT or 'unspecified'}\n\n"
                  f"## Worker summary\n\n{summary[-4000:]}\n\n"
                  f"Generated by a headless worker session. Review before merging.")
    print(f"[worker] PR: {url}", flush=True)


if __name__ == "__main__":
    main()

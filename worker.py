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

def notion(path):
    req = urllib.request.Request(f"https://api.notion.com/v1{path}")
    req.add_header("Authorization", f"Bearer {NOTION_TOKEN}")
    req.add_header("Notion-Version", "2022-06-28")
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

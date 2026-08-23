"""Self-check for the worker's pure logic — URL/slug parsing and Notion block flattening.

Run: python3 test_worker.py   (no network, no repo, no claude)
"""
import os

os.environ.setdefault("NOTION_TOKEN", "x")
os.environ.setdefault("GITHUB_TOKEN", "x")
os.environ.setdefault("SPEC_URL", "https://app.notion.com/p/3c4ac87aab448175ae0ef32a88fe9bda")
import worker as w


def test_page_id_from_url():
    assert w.page_id_from_url("https://app.notion.com/p/3c4ac87aab448175ae0ef32a88fe9bda") \
        == "3c4ac87aab448175ae0ef32a88fe9bda"
    assert w.page_id_from_url("https://notion.so/Spec-Title-abc123?pvs=4") == "abc123"


def test_repo_slug():
    for url in ("https://github.com/licongchen200/my-agents",
                "https://github.com/licongchen200/my-agents.git",
                "git@github.com:licongchen200/my-agents.git"):
        assert w.repo_slug(url) == "licongchen200/my-agents", url


def test_repo_slug_rejects_junk():
    for bad in ("", "not a url", "https://gitlab.com/x/y"):
        try:
            w.repo_slug(bad)
        except RuntimeError:
            continue
        raise AssertionError(f"accepted {bad!r}")


def test_slug_for_makes_safe_branch_names():
    assert w.slug_for("Spec: VPS Setup") == "spec-vps-setup"
    assert w.slug_for("!!!") == "spec"
    assert len(w.slug_for("x" * 200)) <= 40


def test_rich_text_flattening():
    assert w.rich([{"plain_text": "a"}, {"plain_text": "b"}]) == "ab"
    assert w.rich(None) == ""


def test_brief_contains_spec_and_guardrails():
    b = w.build_brief("do the thing")
    assert "do the thing" in b
    assert "Do not open a pull request" in b, "worker must not double-open PRs"
    assert "change nothing and say why" in b, "impossible specs must not invent work"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t(); print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")

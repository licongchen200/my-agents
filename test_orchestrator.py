"""Self-check for the claim/lease logic — the part that loses work when it's wrong.

Run: python3 test_orchestrator.py   (no network, no framework, temp db)
"""
import os
import tempfile
from datetime import timedelta

os.environ.setdefault("WORKER_CMD_INFRA", "true")
import orchestrator as o


def seed(db, spec_url="https://notion.so/spec-1", task_type="infra"):
    db.execute("""INSERT INTO tasks (spec_url, spec_id, name, task_type, status, attempts,
                                     created_at, updated_at)
                  VALUES (?, 1, 'test spec', ?, 'not started', 0, ?, ?)""",
               (spec_url, task_type, o.iso(o.now()), o.iso(o.now())))


def fresh():
    db = o.connect(tempfile.mktemp(suffix=".db"))
    seed(db)
    return db


def test_claim_marks_in_progress_with_lease():
    db = fresh()
    t = o.claim(db, owner="a")
    assert t["status"] == "in progress", t["status"]
    assert t["owner"] == "a"
    assert t["attempts"] == 1
    assert t["lease_expires_at"] > o.iso(o.now())


def test_second_claim_does_not_get_a_live_lease():
    db = fresh()
    assert o.claim(db, owner="a") is not None
    assert o.claim(db, owner="b") is None, "live lease was handed out twice"


def test_expired_lease_is_reclaimed_and_counts_an_attempt():
    db = fresh()
    o.claim(db, owner="a")
    db.execute("UPDATE tasks SET lease_expires_at=?", (o.iso(o.now() - timedelta(seconds=1)),))
    t = o.claim(db, owner="b")
    assert t is not None, "expired lease was not reclaimed"
    assert t["owner"] == "b"
    assert t["attempts"] == 2, t["attempts"]


def test_attempt_limit_marks_failed_and_stops_dispatching():
    db = fresh()
    db.execute("UPDATE tasks SET attempts=?", (o.MAX_ATTEMPTS,))
    db.execute("UPDATE tasks SET status='in progress', lease_expires_at=?",
               (o.iso(o.now() - timedelta(seconds=1)),))
    assert o.claim(db, owner="a") is None, "dispatched past the attempt limit"
    row = db.execute("SELECT status, owner FROM tasks").fetchone()
    assert row["status"] == "failed", row["status"]
    assert row["owner"] is None


def test_unknown_task_type_is_left_alone():
    """ios specs must not be claimed while no Mac worker exists."""
    db = o.connect(tempfile.mktemp(suffix=".db"))
    seed(db, task_type="ios")
    assert o.claim(db, owner="a", task_types=["infra"]) is None
    assert db.execute("SELECT status FROM tasks").fetchone()["status"] == "not started"


def test_backend_routes_to_cloud_not_a_local_command():
    """backend specs must never run a local shell command — a cloud runner handles them.

    If backend ever appears in WORKERS as a command string, work that assumed an ephemeral
    isolated runner would instead execute on the VPS.
    """
    assert o.WORKERS.get("backend") in (None, "cloud"), o.WORKERS.get("backend")


def test_infra_stays_local():
    """infra specs must run on the box, since a cloud runner cannot reach the VPS."""
    assert o.WORKERS.get("infra") == "true"


def test_pull_specs_is_idempotent():
    db = fresh()
    before = db.execute("SELECT attempts, status FROM tasks").fetchone()
    o.claim(db, owner="a")
    seed_again = db.execute(
        """INSERT INTO tasks (spec_url, spec_id, name, task_type, status, attempts,
                              created_at, updated_at)
           VALUES ('https://notion.so/spec-1', 1, 'test spec', 'infra', 'not started', 0, ?, ?)
           ON CONFLICT(spec_url) DO NOTHING""", (o.iso(o.now()), o.iso(o.now())))
    row = db.execute("SELECT attempts, status FROM tasks").fetchone()
    assert row["status"] == "in progress", "re-pull reset a claimed task"
    assert row["attempts"] == 1
    assert before is not None


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")

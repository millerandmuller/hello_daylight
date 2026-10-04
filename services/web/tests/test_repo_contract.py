"""One contract, two stores: the file repo and the Firestore repo (against an in-memory stand-in)."""

import time

import pytest

from app import repo_firestore
from app.repo import FileRepo, NotFound

from .fake_firestore import FakeClient


@pytest.fixture(params=["file", "firestore"])
def store(request, tmp_path, monkeypatch):
    if request.param == "file":
        return FileRepo(tmp_path / "d", tmp_path / "d" / "ledger.jsonl")
    monkeypatch.setattr(repo_firestore.firestore, "transactional", lambda fn: fn)
    return repo_firestore.FirestoreRepo(client=FakeClient())


def test_projects_are_owned_and_mutated_on_a_fresh_read(store):
    a = store.create_project("a@x.example", {"url": "https://a.example", "goals": []})
    b = store.create_project("b@x.example", {"url": "https://b.example", "goals": []})
    assert [p["id"] for p in store.list_projects("a@x.example")] == [a]
    assert {p["id"] for p in store.list_projects()} == {a, b}

    def add(doc):
        doc["goals"].append("one")

    store.update_project(a, add)
    store.update_project(a, add)
    assert store.get_project(a)["goals"] == ["one", "one"]
    assert store.count_projects_since("a@x.example", "2000-01-01T00:00:00") == 1
    assert store.count_projects_since("a@x.example", "2999-01-01T00:00:00") == 0


def test_missing_and_malformed_ids_are_not_found(store):
    for bad in ("doesnotexist", "../../etc/passwd", "a/b", "", "x" * 100):
        with pytest.raises(NotFound):
            store.get_project(bad)
    with pytest.raises(NotFound):
        store.get_run("nope-nope-nope")


def test_runs_checkpoints_and_mutation(store):
    store.create_run({"run_id": "run-aaaaaaaa", "project_id": "p-12345678", "started_at": "2026-10-01T10:00:00+00:00", "cards": []})
    store.create_run({"run_id": "run-bbbbbbbb", "project_id": "p-12345678", "started_at": "2026-10-02T10:00:00+00:00", "cards": []})
    assert [r["run_id"] for r in store.list_runs("p-12345678", 5)] == ["run-bbbbbbbb", "run-aaaaaaaa"]
    store.mutate_run("run-aaaaaaaa", lambda d: d["cards"].append({"id": "k1"}))
    assert store.get_run("run-aaaaaaaa")["cards"] == [{"id": "k1"}]
    assert store.load_checkpoint("run-aaaaaaaa") is None
    store.save_checkpoint("run-aaaaaaaa", {"stage": "scouts"})
    assert store.load_checkpoint("run-aaaaaaaa") == {"stage": "scouts"}


def test_lock_refuses_a_live_holder_and_takes_over_a_dead_one(store):
    assert store.acquire_lock("proj-lock-1", "run-one-aaaa", 90)["acquired"]
    again = store.acquire_lock("proj-lock-1", "run-one-aaaa", 90)
    assert not again["acquired"], "a second start of the SAME run id is a double start too"
    other = store.acquire_lock("proj-lock-1", "run-two-bbbb", 90)
    assert not other["acquired"] and other["holder"] == "run-one-aaaa"
    assert store.heartbeat("proj-lock-1", "run-one-aaaa") is True
    assert store.heartbeat("proj-lock-1", "run-two-bbbb") is False
    store.release_lock("proj-lock-1", "run-two-bbbb")  # not the holder: nothing happens
    assert not store.acquire_lock("proj-lock-1", "run-two-bbbb", 90)["acquired"]
    time.sleep(0.02)
    took = store.acquire_lock("proj-lock-1", "run-two-bbbb", 0.0)
    assert took["acquired"] and took["takeover"] and took["previous"] == "run-one-aaaa"
    store.release_lock("proj-lock-1", "run-two-bbbb")
    assert store.acquire_lock("proj-lock-1", "run-three-cc", 90)["acquired"]
    assert store.heartbeat("proj-lock-1", "run-one-aaaa") is False, "a run that lost its lock learns it"


def test_feedback_is_used_once(store):
    a = store.add_feedback("proj-fb-001", {"kind": "down", "comment": "no"})
    store.add_feedback("proj-fb-001", {"kind": "up", "comment": "yes"})
    store.add_feedback("proj-fb-002", {"kind": "up", "comment": "other project"})
    assert len(store.list_feedback("proj-fb-001")) == 2
    store.mark_feedback("proj-fb-001", [a], "run-x")
    left = store.list_feedback("proj-fb-001", unconsumed_only=True)
    assert [f["comment"] for f in left] == ["yes"]


def test_ledger_is_append_only_and_sums_by_month(store):
    store.append_ledger({"run_id": "r1", "project_id": "p1", "started_at": "2026-10-03T10:00:00+00:00", "cost_eur": 0.40})
    store.append_ledger({"run_id": "r2", "project_id": "p2", "started_at": "2026-10-04T10:00:00+00:00", "cost_eur": 0.25})
    store.append_ledger({"run_id": "r3", "project_id": "p1", "started_at": "2026-09-30T10:00:00+00:00", "cost_eur": 9.0})
    assert store.month_spend("p1", "2026-10") == pytest.approx(0.40)
    assert store.month_spend(None, "2026-10") == pytest.approx(0.65)
    assert [l["run_id"] for l in store.ledger("p1")] == ["r1", "r3"]
    assert len(store.ledger(None, limit=2)) == 2


def test_allowlist_and_settings(store):
    assert store.allow_get("A@X.example") is None
    store.allow_add("A@X.example", "admin")
    assert store.allow_get("a@x.example")["email"] == "a@x.example"
    store.allow_add("b@x.example", "admin")
    assert [a["email"] for a in store.allow_list()] == ["a@x.example", "b@x.example"]
    store.allow_remove("a@x.example")
    assert store.allow_get("a@x.example") is None
    store.set_settings({"global_cap_eur": 10})
    assert store.set_settings({"x": 1}) == {"global_cap_eur": 10, "x": 1}


def test_public_slots_are_counted_released_and_expire(store):
    tokens = [store.acquire_slot(3, 60) for _ in range(3)]
    assert all(tokens) and store.acquire_slot(3, 60) is None
    store.release_slot(tokens[0])
    assert store.acquire_slot(3, 60) is not None
    time.sleep(0.05)
    assert store.acquire_slot(1, 0.01) is not None, "expired slots do not block"

"""Regression tests for what only the live system showed: a platform retry that paid twice, a database that turned
away parallel writes, a failed night that could not be continued, off-topic cards, broken access logs."""

import asyncio
import logging

import pytest
from google.api_core import exceptions as gexc

from app import engine, logsafe, night, service
from app.engine import NightRun, describe_crash
from app.repo import now
from app.service import RunRefused

from .fakes import project_input
from .test_engine import last_run, open_hosts, pid, run, world  # noqa: F401  (fixtures)


def _night(repo, monkeypatch, *argv, attempt="0"):
    monkeypatch.setattr(night, "open_repo", lambda: repo)
    monkeypatch.setenv("CLOUD_RUN_TASK_ATTEMPT", attempt)
    monkeypatch.setattr(service.config, "operator_key", lambda: "x")  # so the gate that refuses is the lock, not a missing key
    args = night.argparse.Namespace(project=argv[0], trigger="manual", budget=None, takeover=False, wait_stale=False, force=False)
    return asyncio.run(night.night(args))


# --- P1-2: one start request never pays for two runs ---------------------------------------------
def test_a_refused_start_exits_0_so_the_platform_does_not_retry(repo, pid, monkeypatch):
    repo.create_run({"run_id": "run-live-0003", "project_id": pid, "owner": "o", "status": "running", "started_at": now(), "cards": []})
    assert repo.acquire_lock(pid, "run-live-0003", 90)["acquired"]
    assert _night(repo, monkeypatch, pid) == 0
    assert [l["reason"] for l in repo.ledger(pid)] == ["already_running"]


def test_a_platform_retry_after_a_finished_run_starts_nothing(repo, pid, world, monkeypatch):
    """The live chain: A runs, B is refused, the platform retries B after A finished -> it must not pay a new run."""
    first, _ = run(repo, pid, world)
    assert first["status"] in ("ok", "partial")
    with pytest.raises(RunRefused) as err:
        run(repo, pid, world, resume_only=True)
    assert err.value.reason == "nothing_to_resume"
    paid = [l for l in repo.ledger(pid) if l["cost_eur"] > 0]
    assert len(paid) == 1, "only the first run cost money"
    assert len(repo.list_runs(pid, 10)) == 1, "the retry created no run"


def test_the_night_entry_point_reads_the_platform_attempt(monkeypatch):
    monkeypatch.setenv("CLOUD_RUN_TASK_ATTEMPT", "1")
    assert night.platform_retry() is True
    monkeypatch.setenv("CLOUD_RUN_TASK_ATTEMPT", "0")
    assert night.platform_retry() is False


def test_a_platform_retry_may_still_continue_a_dead_run(repo, pid, world):
    repo.create_run({"run_id": "run-dead-0003", "project_id": pid, "owner": "o", "status": "running", "started_at": now(), "cards": [], "plan_view": []})
    ledger, _ = run(repo, pid, world, takeover=True, resume_only=True)
    assert ledger["run_id"] == "run-dead-0003"


# --- P2-1 / P2-2: a storage failure is named, resumable, and never paid twice ----------------------
def _contention() -> Exception:
    try:
        raise gexc.Aborted("409 Too much contention on these datastore entities. please try again.")
    except gexc.Aborted as inner:
        try:
            raise ValueError("Failed to commit transaction in 5 attempts.") from inner
        except ValueError as outer:
            return outer


def test_a_contention_crash_is_named_and_resumable():
    reason, resumable = describe_crash(_contention())
    assert reason.startswith("storage error") and resumable
    reason, resumable = describe_crash(KeyError("x"))
    assert reason == "internal error: KeyError" and not resumable


def test_a_view_write_that_fails_does_not_end_the_run(repo, pid, world, monkeypatch):
    calls = {"n": 0}
    real = repo.patch_run

    def flaky(run_id, patch):
        calls["n"] += 1
        if patch.get("status") == "running" and calls["n"] % 2 == 0:
            raise _contention()
        return real(run_id, patch)

    monkeypatch.setattr(repo, "patch_run", flaky)
    ledger, _ = run(repo, pid, world)
    assert ledger["status"] in ("ok", "partial") and ledger["cards"] > 0


def test_a_failed_night_resumes_from_its_checkpoint_and_books_each_euro_once(repo, pid, world, monkeypatch):
    real_save = repo.save_checkpoint
    state = {"n": 0, "armed": True}

    def save(run_id, data):
        done = [t for t in data.get("tasks", {}).values() if t["status"] == "done"]
        if state["armed"] and len(done) >= 3:
            state["armed"] = False
            raise _contention()
        return real_save(run_id, data)

    monkeypatch.setattr(repo, "save_checkpoint", save)
    first, _ = run(repo, pid, world)
    assert first["status"] == "failed" and first["reason"].startswith("storage error")
    doc = last_run(repo, pid)
    assert doc["resumable"] is True and "continues where it stopped" in doc["headline"]
    cp = repo.load_checkpoint(first["run_id"])
    done_before = {t for t, v in cp["tasks"].items() if v["status"] == "done"}
    assert done_before

    second, gw = run(repo, pid, world)
    assert second["run_id"] == first["run_id"], "the same run continues"
    assert ("plan", "lead_planner") not in gw.calls, "the plan is not paid again"
    assert not ({n.removeprefix("scout_") for s, n in gw.calls if s == "scout"} & done_before), "finished scouts are not paid again"
    assert second["status"] in ("ok", "partial") and second["cards"] > 0
    lines = [l for l in repo.ledger(pid) if l["run_id"] == first["run_id"]]
    assert len(lines) == 2
    assert abs(sum(l["cost_eur"] for l in lines) - second["cost_total_eur"]) < 1e-6, "monthly sums count every euro once"
    assert second["booked_before_eur"] == pytest.approx(first["cost_eur"])


def test_a_failed_run_stays_failed_after_too_many_resumes(repo, pid):
    run_doc = {"run_id": "run-fail-0001", "project_id": pid, "status": "failed", "resumable": False, "started_at": now()}
    assert service.resumable_run(run_doc) is None
    assert service.resumable_run({**run_doc, "resumable": True}) == "run-fail-0001"
    assert service.resumable_run({**run_doc, "resumable": True, "started_at": "2020-01-01T00:00:00+00:00"}) is None


def test_money_in_flight_at_a_hard_kill_is_booked_on_resume():
    from app import config
    from app.meter import RunMeter

    m = RunMeter(config.contract_for("private"), {"spent_eur": 0.10, "in_flight_eur": 0.02})
    assert m.book_lost_in_flight({"spent_eur": 0.10, "in_flight_eur": 0.02}) == pytest.approx(0.02)
    assert m.spent_eur == pytest.approx(0.12)


# --- P2-3: an off-topic finding does not become a card ----------------------------------------------
def test_the_curator_s_low_relevance_picks_are_dropped_and_nothing_is_topped_up(repo, pid, world):
    from app.schemas import Curation, Pick

    def setup(gw):
        original = gw.run_agent

        async def run_agent(**kw):
            if kw.get("step") == "curate":
                ids = [l.split(" ", 1)[0] for l in kw["user_text"].splitlines() if l[:1] == "c" and l.split(" ", 1)[0][1:].isdigit()]
                picks = [Pick(item_id=ids[0], relevance=9, fit="fits"), Pick(item_id=ids[1], relevance=2, fit="another topic")]
                from .fakes import AgentResult

                gw.calls.append(("curate", "curator"))
                return AgentResult(parsed=Curation(picks=picks))
            return await original(**kw)

        gw.run_agent = run_agent

    ledger, _ = run(repo, pid, world, gw_setup=setup)
    doc = last_run(repo, pid)
    assert len(doc["cards"]) == 1, "only the relevant pick became a card; nothing was topped up"
    assert "not close enough" in doc["headline"]


def test_enforce_picks_tops_up_only_when_the_curator_did_not_answer():
    items = [{"item_id": f"c{i}", "kind": "question", "author": "", "url": f"https://x.example/{i}"} for i in range(1, 8)]
    assert NightRun._enforce_picks(["c2"], items, 5, fill=False) == ["c2"]
    assert len(NightRun._enforce_picks(["c2"], items, 5, fill=True)) == 5


# --- P3: access logs keep their shape ---------------------------------------------------------------
def test_uvicorn_access_lines_survive_the_key_scrubber():
    from uvicorn.logging import AccessFormatter

    logsafe.install()
    rec = logging.getLogger("uvicorn.access").makeRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d', ("1.2.3.4:5", "GET", "/healthz", "1.1", 200), None)
    logsafe.scrub_record(rec)
    assert '"GET /healthz HTTP/1.1" 200' in AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s').format(rec)
    rec = logging.getLogger("x").makeRecord("x", logging.INFO, __file__, 1, "key %s", ("AIzaSyA1234567890123456789012345",), None)
    logsafe.scrub_record(rec)
    assert "AIza" not in rec.getMessage()


# --- P3: a workspace can be paused and deleted ------------------------------------------------------
def test_a_paused_workspace_is_left_out_of_the_night_and_a_deleted_one_is_gone(repo, pid, world, monkeypatch):
    from app import cli

    monkeypatch.setattr(cli, "open_repo", lambda: repo)
    assert cli.main(["pause", pid]) == 0
    assert repo.get_project(pid)["nightly"] is False
    run(repo, pid, world)
    lines_before = len(repo.ledger(pid))
    assert cli.main(["delete", pid]) == 1, "without --yes nothing is deleted"
    assert cli.main(["delete", pid, "--yes"]) == 0
    assert repo.list_runs(pid, 10) == [] and repo.list_projects() == []
    assert len(repo.ledger(pid)) == lines_before, "the accounting stays"


def test_logout_from_another_site_is_refused(client):
    r = client.post("/auth/logout", headers={"origin": "https://evil.example", "sec-fetch-site": "cross-site"}, follow_redirects=False)
    assert r.status_code == 403

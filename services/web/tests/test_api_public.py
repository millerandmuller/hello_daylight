"""The public mode: the visitor's key stays in memory, nothing is stored, a bad key never reaches a paid call."""

import asyncio
import json
import logging
import pathlib

import pytest

from app import config, main
from app.engine import ProjectInput

KEY = "AIza-test-0123456789abcdefghijkl"
H = {"X-Gemini-Key": KEY}
PROJECT = {"name": "Notecast", "url": "https://notecast.example", "one_liner": "Turns notes into issues.", "audience": "writers", "goals": ["First users"], "feedback": [], "seen_urls": []}


@pytest.fixture(autouse=True)
def fresh_limits():
    main._run_window.hits.clear()
    main._intake_window.hits.clear()
    main._check_window.hits.clear()


def scripted_run(events=None, sleep=0.0, record=None):
    """A stand-in for service.run_public that never calls a model but behaves like it: emits events, ends with 'done'."""

    async def run_public(project, api_key, emit, *, budget_override=None, ledger_repo=None, http=None, gateway=None):
        if record is not None:
            record.update(project=project, key=api_key, budget=budget_override)
        emit({"type": "plan", "tasks": [{"id": "t1", "role": "Scout", "kind": "question", "status": "pending", "found": 0, "instruction": "x", "rationale": "y"}], "feedback_note": ""})
        await asyncio.sleep(sleep)
        for e in events or []:
            emit(e)
        ledger = {"run_id": "r1", "mode": "public", "status": "ok", "cost_eur": 0.1, "calls": 3}
        if ledger_repo is not None:
            ledger_repo.append_ledger({**ledger, "project_id": None, "started_at": "2026-10-04T10:00:00+00:00"})
        emit({"type": "done", "ledger": ledger, "headline": "", "cost_line": "Cost of this run: 0.10 EUR of 1.00 EUR, 3 model calls."})
        return ledger

    return run_public


def read_sse(resp):
    return [json.loads(l[6:]) for l in resp.text.splitlines() if l.startswith("data: ")]


# --- the key ------------------------------------------------------------------------------------------
def test_a_bad_key_is_refused_before_anything_paid_happens(anon, calls):
    r = anon.post("/api/public/key-check", headers={"X-Gemini-Key": "nope"})
    assert r.status_code == 401 and r.json()["error"] == "Key invalid."
    r = anon.post("/api/public/intake", headers={"X-Gemini-Key": "nope"}, json={"url": "https://example.com"})
    assert r.status_code == 401 and calls["propose"] == [] and calls["fetch"] == []
    main.app.state.run_public = scripted_run(record={})
    r = anon.post("/api/public/run", headers={"X-Gemini-Key": "nope"}, json={"project": PROJECT})
    assert r.status_code == 401


def test_a_missing_key_is_refused(anon):
    assert anon.post("/api/public/key-check").status_code == 401
    assert anon.post("/api/public/run", json={"project": PROJECT}).status_code == 401


def test_a_key_check_that_cannot_be_answered_is_a_clear_retry_not_a_run(anon):
    async def unsure(key):
        return None

    main.app.state.check_key = unsure
    r = anon.post("/api/public/run", headers=H, json={"project": PROJECT})
    assert r.status_code == 503 and "could not be checked" in r.json()["error"]


# --- intake -----------------------------------------------------------------------------------------------
def test_public_intake_uses_the_visitors_key_and_stores_nothing(anon, repo, calls):
    r = anon.post("/api/public/intake", headers=H, json={"url": "example.com", "goals": "Beta testers"})
    assert r.status_code == 200
    body = r.json()
    assert body["card"]["name"] == "Notecast" and any(g["origin"] == "user" for g in body["goals"])
    assert calls["propose"][0]["api_key"] == KEY
    assert repo.list_projects() == []
    assert KEY not in json.dumps(body)
    line = repo.ledger()[0]
    assert line["mode"] == "public" and line["project_id"] is None and KEY not in json.dumps(line)


def test_unreadable_page_in_public_mode_asks_for_a_sentence(anon):
    from app import fetcher

    def broken(url):
        raise fetcher.FetchError("The page took too long to answer.")

    main.app.state.fetch_page = broken
    r = anon.post("/api/public/intake", headers=H, json={"url": "https://example.com"})
    assert r.status_code == 422 and r.json()["need_description"] is True


# --- the run --------------------------------------------------------------------------------------------------
def test_a_public_run_streams_events_and_the_key_is_not_in_them(anon, repo):
    record = {}
    main.app.state.run_public = scripted_run(events=[{"type": "card", "card": {"id": "k1", "title": "T"}}], record=record)
    r = anon.post("/api/public/run", headers=H, json={"project": PROJECT})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    events = read_sse(r)
    assert [e["type"] for e in events] == ["plan", "card", "done"]
    assert KEY not in r.text
    assert record["key"] == KEY and record["budget"] is None
    assert isinstance(record["project"], ProjectInput) and record["project"].goals == ["First users"]
    assert repo.list_projects() == [] and not list((repo.root / "runs").glob("*.json"))
    assert repo.acquire_slot(1, 60) is not None, "the slot was given back"


def test_the_run_needs_a_goal_and_a_sane_body(anon):
    main.app.state.run_public = scripted_run()
    assert anon.post("/api/public/run", headers=H, json={"project": {**PROJECT, "goals": []}}).status_code == 422
    assert anon.post("/api/public/run", headers=H, json={"project": {**PROJECT, "name": "x" * 500}}).status_code == 422
    assert anon.post("/api/public/run", headers=H, json={"project": PROJECT, "budget": 1000}).status_code == 422
    assert anon.post("/api/public/run", headers=H, json={"project": {**PROJECT, "goals": ["g"] * 21}}).status_code == 422
    big = anon.post("/api/public/run", headers={**H, "content-length": "999999"}, content=b"x" * 10)
    assert big.status_code == 413


def test_a_lowered_budget_goes_through_and_is_never_raised_by_the_visitor(anon):
    record = {}
    main.app.state.run_public = scripted_run(record=record)
    anon.post("/api/public/run", headers=H, json={"project": PROJECT, "budget": 0.2})
    assert record["budget"] == 0.2
    assert config.contract_for("public", 0.2).budget_eur == 0.2 and config.contract_for("public", 50).budget_eur == 1.0


def test_at_most_three_public_runs_at_once(anon, repo):
    held = [repo.acquire_slot(3, 600) for _ in range(3)]
    assert all(held)
    main.app.state.run_public = scripted_run()
    r = anon.post("/api/public/run", headers=H, json={"project": PROJECT})
    assert r.status_code == 429 and "Three runs" in r.json()["error"]


def test_one_address_gets_a_limited_number_of_runs_per_hour(anon):
    main.app.state.run_public = scripted_run()
    codes = [anon.post("/api/public/run", headers=H, json={"project": PROJECT}).status_code for _ in range(config.PUBLIC_RATE_PER_IP_PER_HOUR + 1)]
    assert codes[:-1] == [200] * config.PUBLIC_RATE_PER_IP_PER_HOUR and codes[-1] == 429


def test_closing_the_tab_stops_the_run_and_frees_the_slot(anon, repo):
    """Needs a real server: the test client cannot simulate a browser that goes away."""
    import socket
    import threading
    import time

    import httpx
    import uvicorn

    state = {"cancelled": False}

    async def forever(project, api_key, emit, **kw):
        emit({"type": "plan", "tasks": [], "feedback_note": ""})
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise

    main.app.state.run_public = forever
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    try:
        with httpx.stream("POST", f"http://127.0.0.1:{port}/api/public/run", headers=H, json={"project": PROJECT}, timeout=10) as r:
            for line in r.iter_lines():
                if line.startswith("data: "):
                    break  # the visitor leaves after the first event
        for _ in range(100):
            if state["cancelled"]:
                break
            time.sleep(0.05)
    finally:
        server.should_exit = True
        thread.join(timeout=5)
    assert state["cancelled"], "the run was cancelled when the connection closed"
    assert repo.acquire_slot(1, 60) is not None


# --- the key never lands anywhere ----------------------------------------------------------------------------
def test_the_key_is_in_no_log_no_file_and_no_response(anon, repo, caplog, tmp_path):
    caplog.set_level(logging.DEBUG)

    def leaky(page, description, user_goals, api_key=None):
        raise RuntimeError(f"upstream said: invalid key {api_key} in request")

    main.app.state.propose = leaky
    r = anon.post("/api/public/intake", headers=H, json={"url": "https://example.com"})
    assert r.status_code == 200 and r.json()["proposal_error"]
    main.app.state.run_public = scripted_run()
    anon.post("/api/public/run", headers=H, json={"project": PROJECT})
    assert KEY not in caplog.text and KEY not in r.text
    for path in pathlib.Path(repo.root).rglob("*"):
        if path.is_file():
            assert KEY.encode() not in path.read_bytes(), path
    assert KEY[:8].encode() not in pathlib.Path(repo.ledger_file).read_bytes()


def test_the_log_scrubber_removes_anything_shaped_like_a_key():
    from app import logsafe

    assert "AIzaSyDUMMYDUMMYDUMMYDUMMYDUMMY" not in logsafe.scrub("failed with AIzaSyDUMMYDUMMYDUMMYDUMMYDUMMY here")
    assert "secret-value" not in logsafe.scrub("x-gemini-key: secret-value")


def test_the_public_page_loads_only_own_scripts_and_says_where_the_key_lives(anon):
    html = anon.get("/try").text
    assert "stays in this browser" in html or "stay in this browser" in html
    assert "/static/public.js" in html and "https://" not in " ".join(__import__("re").findall(r'<script[^>]*src="([^"]+)"', html))
    js = pathlib.Path(main.HERE / "static" / "public.js").read_text()
    assert "innerHTML" not in js, "model and page text are only ever set as text"
    assert "document.cookie" not in js and "console.log" not in js

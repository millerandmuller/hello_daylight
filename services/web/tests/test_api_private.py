"""The private mode over HTTP: who gets in (401/403), whose project it is, running, cards, admin, caps."""

import re
import time

import pytest
from fastapi.testclient import TestClient

from app import auth, config, main
from app.repo import now

from .conftest import OWNER, sign_in

HX = {"HX-Request": "true"}
PRIVATE_GETS = ["/app", "/p/AAAAAAAAAAAAAAAAAAAAAA", "/p/AAAAAAAAAAAAAAAAAAAAAA/stream", "/p/AAAAAAAAAAAAAAAAAAAAAA/run", "/p/AAAAAAAAAAAAAAAAAAAAAA/inbox", "/admin"]
PRIVATE_POSTS = [("/intake", {"url": "https://example.com"}), ("/p/AAAAAAAAAAAAAAAAAAAAAA/run", {}), ("/p/AAAAAAAAAAAAAAAAAAAAAA/confirm", {}), ("/p/AAAAAAAAAAAAAAAAAAAAAA/cards/k1/sign", {}), ("/admin/allow", {"email": "x@y.z"})]


def make_project(client, goals_confirmed=True):
    r = client.post("/intake", data={"url": "https://example.com", "goals": "First users"}, follow_redirects=False)
    token = re.search(r"/p/([A-Za-z0-9_-]{22})", r.headers["location"]).group(1)
    if goals_confirmed:
        client.post(f"/p/{token}/confirm", headers=HX)
    return token


# --- T10: no login is 401, a foreign account is 403, and nothing is spent either way ---------------
@pytest.mark.parametrize("path", PRIVATE_GETS)
def test_no_login_is_401_on_private_pages(anon, path):
    assert anon.get(path).status_code == 401
    assert anon.get(path, headers={"accept": "text/html"}).status_code == 401


@pytest.mark.parametrize("path,data", PRIVATE_POSTS)
def test_no_login_is_401_on_private_actions_and_nothing_runs(anon, calls, path, data):
    r = anon.post(path, data=data)
    assert r.status_code == 401
    assert calls["launch"] == [] and calls["propose"] == [] and calls["fetch"] == []


def test_a_google_account_outside_the_allowlist_gets_403_and_no_model_call(anon, calls):
    r = anon.post("/auth/session", json={"id_token": "dev:stranger@example.com"})
    assert r.status_code == 403
    # even with a hand-made session cookie for that account, every private route says 403
    anon.cookies.set(auth.COOKIE, auth.make_session("stranger@example.com", "u1"))
    for path in PRIVATE_GETS[:5]:
        assert anon.get(path).status_code == 403, path
    for path, data in PRIVATE_POSTS[:4]:
        assert anon.post(path, data=data).status_code == 403, path
    assert calls["launch"] == [] and calls["propose"] == [] and calls["fetch"] == []


def test_removing_someone_from_the_allowlist_takes_effect_on_the_next_request(client, repo):
    assert client.get("/app").status_code == 200
    repo.allow_remove(OWNER)
    assert client.get("/app").status_code == 403


def test_a_forged_or_expired_session_is_401(anon):
    anon.cookies.set(auth.COOKIE, "eyJ4IjoxfQ.deadbeef")
    assert anon.get("/app").status_code == 401
    old = auth.make_session(OWNER, "u", now=time.time() - 13 * 3600)
    anon.cookies.set(auth.COOKIE, old)
    assert anon.get("/app").status_code == 401


def test_a_cross_site_post_with_the_cookie_is_refused(client):
    r = client.post("/intake", data={"url": "https://example.com"}, headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403


def test_the_session_cookie_is_http_only_and_same_site(anon, repo):
    repo.allow_add(OWNER, "t")
    r = anon.post("/auth/session", json={"id_token": f"dev:{OWNER}"})
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie


def test_the_development_login_cannot_run_in_the_cloud(monkeypatch):
    monkeypatch.setattr(config, "IS_CLOUD", True)
    monkeypatch.setattr(config, "AUTH_MODE", "dev")
    with pytest.raises(RuntimeError):
        auth.assert_safe_config()
    monkeypatch.setattr(config, "AUTH_MODE", "firebase")
    monkeypatch.setattr(config, "SESSION_SECRET", "short")
    with pytest.raises(RuntimeError):
        auth.assert_safe_config()
    with pytest.raises(auth.AuthError):
        monkeypatch.setattr(config, "IS_CLOUD", True)
        monkeypatch.setattr(config, "AUTH_MODE", "dev")
        auth.verify_id_token("dev:me@example.com")


def test_security_headers_on_every_page(client):
    r = client.get("/app")
    assert "script-src 'self'" in r.headers["content-security-policy"] and "connect-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["referrer-policy"] == "no-referrer"
    login = client.get("/login").headers["content-security-policy"]
    assert "identitytoolkit.googleapis.com" in login and "script-src 'self'" in login
    assert "unsafe-inline" not in login and "unsafe-eval" not in login


def test_no_page_loads_a_script_from_another_server(client):
    token = make_project(client)
    for path in ("/", "/try", "/login", "/app", f"/p/{token}", f"/p/{token}/stream", f"/p/{token}/run", "/self-host"):
        html = client.get(path).text
        for src in re.findall(r'<script[^>]*\ssrc="([^"]+)"', html):
            assert src.startswith("/static/"), (path, src)
        assert "<script>" not in html and "onclick=" not in html, path


# --- ownership -------------------------------------------------------------------------------------
def test_another_account_cannot_see_or_change_my_project(client, repo):
    token = make_project(client)
    repo.allow_add("other@example.com", "t")
    other = sign_in(TestClient(main.app), "other@example.com")
    assert other.get(f"/p/{token}").status_code == 404
    assert other.post(f"/p/{token}/run").status_code == 404
    assert other.post(f"/p/{token}/confirm", headers=HX).status_code == 404
    assert other.get(f"/p/{token}/stream").status_code == 404
    assert repo.get_project(token)["owner"] == OWNER


def test_three_new_projects_a_day_is_the_limit(client):
    for _ in range(3):
        assert client.post("/intake", data={"url": "https://example.com"}, follow_redirects=False).status_code == 303
    assert client.post("/intake", data={"url": "https://example.com"}).status_code == 429


# --- running ----------------------------------------------------------------------------------------
def test_run_needs_confirmed_goals(client, calls):
    token = make_project(client, goals_confirmed=False)
    assert client.post(f"/p/{token}/run").status_code == 409
    assert calls["launch"] == []


def test_run_starts_a_job_and_a_lowered_budget_goes_with_it(client, calls, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-operator-test")
    token = make_project(client)
    r = client.post(f"/p/{token}/run", data={"budget": "0,05"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/p/{token}/stream"
    assert calls["launch"] == [(token, 0.05)]
    assert client.post(f"/p/{token}/run", data={"budget": "lots"}).status_code == 422


def test_a_second_start_while_a_run_is_alive_is_refused(client, repo, calls, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-operator-test")
    token = make_project(client)
    repo.create_run({"run_id": "run-live-0001", "project_id": token, "owner": OWNER, "status": "running", "started_at": now(), "updated_at": now(), "cards": []})
    r = client.post(f"/p/{token}/run")
    assert r.status_code == 409 and "already in progress" in r.text and calls["launch"] == []


def test_a_stale_run_can_be_resumed_from_the_desk(client, repo, calls, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-operator-test")
    token = make_project(client)
    repo.create_run({"run_id": "run-dead-0001", "project_id": token, "owner": OWNER, "status": "running", "started_at": "2026-10-01T00:00:00+00:00", "updated_at": "2026-10-01T00:00:00+00:00", "cards": []})
    page = client.get(f"/p/{token}").text
    assert "Resume the run" in page
    assert client.post(f"/p/{token}/run", follow_redirects=False).status_code == 303


def test_the_monthly_cap_stops_a_click_before_a_job_starts(client, repo, calls, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-operator-test")
    token = make_project(client)
    repo.append_ledger({"run_id": "x", "project_id": token, "started_at": now(), "cost_eur": 25.0, "status": "ok"})
    r = client.post(f"/p/{token}/run")
    assert r.status_code == 409 and "monthly cap" in r.text and calls["launch"] == []


# --- the desk: sign, edit, thumbs ------------------------------------------------------------------------
def with_cards(client, repo):
    token = make_project(client)
    card = {"id": "k1", "rank": 0, "kind": "question", "title": "Where do you find users?", "url": "https://news.ycombinator.com/item?id=1", "date": "2026-10-01", "date_basis": "api",
            "source": "hn", "author": "asker", "quote": "where do you find users", "why": "A real question.", "contact_route": None, "contact_source_url": None,
            "draft": "A first paragraph.\n\nI made it.", "original_draft": "A first paragraph.\n\nI made it.", "needs_attention": [], "state": "new", "thumb": None, "comment": ""}
    repo.create_run({"run_id": "run-done-0001", "project_id": token, "owner": OWNER, "status": "ok", "started_at": now(), "cards": [card], "plan_view": [], "headline": "", "feedback_note": "You said no ads.", "cost_line": "Cost of this run: 0.20 EUR of 1.50 EUR, 60 model calls."})
    return token


def test_the_desk_shows_the_feedback_line_first_then_the_cards(client, repo):
    token = with_cards(client, repo)
    html = client.get(f"/p/{token}").text
    assert html.index("Changed because of your feedback") < html.index("Where do you find users?")
    assert "You said no ads." in html and "Open source" in html and 'rel="noopener noreferrer"' in html


def test_signing_puts_the_draft_in_the_inbox_and_sends_nothing(client, repo, calls):
    token = with_cards(client, repo)
    client.post(f"/p/{token}/cards/k1/sign", headers=HX)
    assert repo.get_run("run-done-0001")["cards"][0]["state"] == "signed"
    inbox = client.get(f"/p/{token}/inbox").text
    assert "A first paragraph." in inbox
    assert calls["fetch"] == [] or all("example.com" in u for u in calls["fetch"])
    client.post(f"/p/{token}/cards/k1/unsign", headers=HX)
    assert repo.get_run("run-done-0001")["cards"][0]["state"] == "new"


def test_an_edit_keeps_the_original_and_becomes_feedback(client, repo):
    token = with_cards(client, repo)
    client.post(f"/p/{token}/cards/k1/edit", data={"text": "Shorter.\n\nMine."}, headers=HX)
    card = repo.get_run("run-done-0001")["cards"][0]
    assert card["draft"] == "Shorter.\n\nMine." and card["original_draft"].startswith("A first paragraph") and card["edited"]
    fb = repo.list_feedback(token, unconsumed_only=True)
    assert fb[0]["kind"] == "edit" and fb[0]["original"].startswith("A first paragraph") and fb[0]["final"] == "Shorter.\n\nMine."


def test_a_thumb_with_a_comment_is_feedback_for_the_next_night(client, repo):
    token = with_cards(client, repo)
    client.post(f"/p/{token}/cards/k1/down", data={"comment": "  Too   salesy "}, headers=HX)
    card = repo.get_run("run-done-0001")["cards"][0]
    assert card["thumb"] == "down" and card["comment"] == "Too salesy"
    fb = repo.list_feedback(token, unconsumed_only=True)
    assert fb[0]["kind"] == "down" and fb[0]["comment"] == "Too salesy" and fb[0]["card_url"].endswith("item?id=1")


def test_unknown_card_or_action_is_404(client, repo):
    token = with_cards(client, repo)
    assert client.post(f"/p/{token}/cards/k9/sign").status_code == 404
    assert client.post(f"/p/{token}/cards/k1/send").status_code == 404


def test_html_in_a_card_is_escaped(client, repo):
    token = with_cards(client, repo)
    repo.mutate_run("run-done-0001", lambda d: d["cards"][0].update(title="<img src=x onerror=alert(1)>", draft="<script>alert(1)</script>"))
    html = client.get(f"/p/{token}").text
    assert "<img src=x" not in html and "<script>alert" not in html


def test_the_stream_page_shows_the_crew_with_reasons(client, repo):
    token = make_project(client)
    repo.create_run({"run_id": "run-view-0001", "project_id": token, "owner": OWNER, "status": "running", "started_at": now(), "updated_at": now(), "cards": [], "feedback_note": "",
                     "plan_view": [{"id": "t1", "role": "Scout A", "kind": "question", "instruction": "Look at HN.", "rationale": "Covers HN.", "tools": ["hn_search"], "status": "running", "found": 0, "reason": "",
                                    "history": [{"reason": "Only ads.", "new_instruction": "Try Ask HN.", "found": 1}], "dropped": [{"url": "https://x.example", "reason": "duplicate"}]}]})
    html = client.get(f"/p/{token}/stream").text
    assert "Scout A" in html and "Covers HN." in html and "Only ads." in html and "Try Ask HN." in html and "duplicate" in html
    assert 'hx-trigger="every 3s"' in html, "polls while the run is alive"


def test_a_stopped_run_is_said_plainly_and_marked_incomplete(client, repo):
    token = make_project(client)
    repo.create_run({"run_id": "run-stop-0001", "project_id": token, "owner": OWNER, "status": "stopped", "started_at": now(), "cards": [], "plan_view": [], "headline": "Run stopped: budget reached.", "incomplete": True,
                     "unfinished": [{"title": "A found thread", "url": "https://news.ycombinator.com/item?id=7", "date": "2026-10-02", "source": "hn", "kind": "question", "why": "w"}]})
    html = client.get(f"/p/{token}").text
    assert "Run stopped: budget reached." in html and "incomplete" in html and "A found thread" in html


def test_run_and_cost_page_shows_budgets_and_the_ledger(client, repo):
    token = make_project(client)
    repo.append_ledger({"run_id": "r1", "project_id": token, "mode": "private", "trigger": "schedule", "status": "ok", "started_at": now(), "cost_eur": 0.31, "calls": 70, "replacements": 2, "reason": "", "resumed_at": []})
    html = client.get(f"/p/{token}/run").text
    assert "0.31 EUR" in html and "1.50 EUR" in html and "schedule" in html


# --- admin ---------------------------------------------------------------------------------------------------
def test_only_the_operator_reaches_admin(client, repo):
    assert client.get("/admin").status_code == 403
    assert client.post("/admin/allow", data={"email": "x@y.z"}).status_code == 403
    admin = sign_in(TestClient(main.app), "admin@example.com")
    assert admin.get("/admin").status_code == 200
    admin.post("/admin/allow", data={"email": "New@Example.com"})
    assert repo.allow_get("new@example.com")
    admin.post("/admin/allow/remove", data={"email": "new@example.com"})
    assert repo.allow_get("new@example.com") is None
    admin.post("/admin/budget", data={"global_cap_eur": "12.5"})
    assert repo.get_settings()["global_cap_eur"] == 12.5


def test_admin_can_stop_a_run(client, repo):
    token = make_project(client)
    repo.create_run({"run_id": "run-stoppable", "project_id": token, "status": "running", "started_at": now(), "updated_at": now(), "cards": []})
    admin = sign_in(TestClient(main.app), "admin@example.com")
    admin.post("/admin/cancel", data={"run_id": "run-stoppable"})
    assert repo.get_run("run-stoppable")["cancel_requested"] is True

import re

from app import main
from app.store import MAX_GOALS

HX = {"HX-Request": "true"}


def _project(client, goals=""):
    r = client.post("/intake", data={"url": "https://example.com", "goals": goals}, follow_redirects=False)
    return re.search(r"/p/([A-Za-z0-9_-]{22})", r.headers["location"]).group(1)


def _goals(token):
    return main.app.state.store.get(token)["goals"]


def test_accept_edit_remove_restore(client):
    token = _project(client)
    g1, g2 = _goals(token)
    assert g1["status"] == g2["status"] == "proposed"

    r = client.post(f"/p/{token}/goals/{g1['id']}/accept", headers=HX)
    assert r.status_code == 200 and 'id="goals"' in r.text and "<html" not in r.text
    assert _goals(token)[0]["status"] == "accepted"

    client.post(f"/p/{token}/goals/{g2['id']}/edit", data={"text": "Feedback from five newsletter writers"}, headers=HX)
    edited = _goals(token)[1]
    assert edited["status"] == "edited"
    assert edited["text"] == "Feedback from five newsletter writers"
    assert edited["original_text"] == "Feedback from newsletter writers"

    client.post(f"/p/{token}/goals/{g1['id']}/remove", headers=HX)
    assert _goals(token)[0]["status"] == "removed"
    client.post(f"/p/{token}/goals/{g1['id']}/restore", headers=HX)
    assert _goals(token)[0]["status"] == "accepted"


def test_edit_with_empty_text_changes_nothing(client):
    token = _project(client)
    g = _goals(token)[0]
    client.post(f"/p/{token}/goals/{g['id']}/edit", data={"text": "   "}, headers=HX)
    assert _goals(token)[0]["text"] == g["text"]


def test_add_goal_without_js_redirects(client):
    token = _project(client)
    r = client.post(f"/p/{token}/goals", data={"text": "  Beta   testers in DACH "}, follow_redirects=False)
    assert r.status_code == 303
    added = _goals(token)[-1]
    assert (added["text"], added["origin"], added["status"]) == ("Beta testers in DACH", "user", "accepted")


def test_goal_limit(client):
    token = _project(client)
    for i in range(MAX_GOALS):
        client.post(f"/p/{token}/goals", data={"text": f"Goal {i}"}, headers=HX)
    active = [g for g in _goals(token) if g["status"] != "removed"]
    assert len(active) == MAX_GOALS
    r = client.post(f"/p/{token}/goals", data={"text": "One too many"}, headers=HX)
    assert "is the limit" in r.text


def test_long_goal_is_truncated(client):
    token = _project(client)
    client.post(f"/p/{token}/goals", data={"text": "x" * 5000}, headers=HX)
    assert len(_goals(token)[-1]["text"]) == 200


def test_confirm_takes_only_what_the_owner_accepted(client):
    token = _project(client, goals="Customers for the Pro plan")
    goals = _goals(token)
    assert [g["status"] for g in goals] == ["accepted", "proposed", "proposed"]
    client.post(f"/p/{token}/goals/{goals[2]['id']}/accept", headers=HX)
    r = client.post(f"/p/{token}/confirm", headers=HX)
    assert "Confirmed" in r.text and "2 goals" in r.text
    project = main.app.state.store.get(token)
    assert [g["text"] for g in project["confirmed_goals"]] == ["Customers for the Pro plan", goals[2]["text"]]
    assert [g["origin"] for g in project["confirmed_goals"]] == ["user", "agent"]
    assert [g["status"] for g in project["goals"]] == ["accepted", "proposed", "accepted"]  # the unanswered suggestion stays a suggestion

    r = client.post(f"/p/{token}/goals", data={"text": "Podcast interview"}, headers=HX)
    assert "changed the goals since confirming" in r.text


def test_confirm_with_nothing_accepted_is_refused_and_says_why(client):
    token = _project(client)
    assert all(g["status"] == "proposed" for g in _goals(token))
    page = client.get(f"/p/{token}").text
    assert "These goals steer the whole night." in page and "<button type=\"button\" disabled>Confirm</button>" in page
    r = client.post(f"/p/{token}/confirm", headers=HX)
    assert "Accept at least one suggestion" in r.text
    assert main.app.state.store.get(token).get("confirmed_goals") is None


def test_a_workspace_from_before_the_change_reads_without_error(client):
    token = _project(client)
    old = {"id": "g_old", "text": "First users", "origin": "agent", "reason": "r", "status": "accepted", "created_at": "2026-10-01T00:00:00"}

    def mutate(d):
        d["goals"] = [old]
        d["confirmed_goals"] = [{"id": "g_old", "text": "First users", "origin": "agent", "reason": "r"}]
        d.pop("pitch_line", None)
        d.pop("pitch_confirmed_at", None)
        d["card"].pop("problem", None)

    main.app.state.store.update(token, mutate)
    page = client.get(f"/p/{token}")
    assert page.status_code == 200 and "Confirmed." in page.text
    assert "No confirmed project sentence yet. Add one and every draft uses your words." in page.text


def test_confirm_all_is_the_operator_and_test_path(client):
    token = _project(client)
    project = main.app.state.store.confirm_goals(token, accept_all=True)
    assert len(project["confirmed_goals"]) == 2


def test_unknown_goal_or_action_is_404(client):
    token = _project(client)
    assert client.post(f"/p/{token}/goals/g_nope/accept").status_code == 404
    g = _goals(token)[0]
    assert client.post(f"/p/{token}/goals/{g['id']}/delete").status_code == 404

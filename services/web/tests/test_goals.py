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


def test_confirm_accepts_proposed_and_snapshots(client):
    token = _project(client, goals="Customers for the Pro plan")
    goals = _goals(token)
    client.post(f"/p/{token}/goals/{goals[1]['id']}/remove", headers=HX)
    r = client.post(f"/p/{token}/confirm", headers=HX)
    assert "Confirmed" in r.text and "2 goals" in r.text
    project = main.app.state.store.get(token)
    assert [g["text"] for g in project["confirmed_goals"]] == ["Customers for the Pro plan", goals[2]["text"]]
    assert [g["origin"] for g in project["confirmed_goals"]] == ["user", "agent"]
    assert all(g["status"] in ("accepted", "removed") for g in project["goals"])

    r = client.post(f"/p/{token}/goals", data={"text": "Podcast interview"}, headers=HX)
    assert "changed the goals since confirming" in r.text


def test_confirm_empty_list_lets_planner_decide(client):
    token = _project(client)
    for g in _goals(token):
        client.post(f"/p/{token}/goals/{g['id']}/remove", headers=HX)
    assert "let the planner decide" in client.get(f"/p/{token}").text
    r = client.post(f"/p/{token}/confirm", headers=HX)
    assert "goals the planner picks and explains" in r.text
    assert main.app.state.store.get(token)["confirmed_goals"] == []


def test_unknown_goal_or_action_is_404(client):
    token = _project(client)
    assert client.post(f"/p/{token}/goals/g_nope/accept").status_code == 404
    g = _goals(token)[0]
    assert client.post(f"/p/{token}/goals/{g['id']}/delete").status_code == 404

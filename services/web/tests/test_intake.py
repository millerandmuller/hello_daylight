import re

from app import fetcher, main
from app.fetcher import FetchError

from .conftest import make_page, make_proposal


def _token(resp):
    return re.search(r"/p/([A-Za-z0-9_-]{22})", resp.headers["location"]).group(1)


def _submit(client, **form):
    return client.post("/intake", data=form, follow_redirects=False)


def test_intake_form_renders(client):
    r = client.get("/app")
    assert r.status_code == 200
    assert "Link to your project" in r.text


def test_front_door_is_open_and_says_what_it_does(anon):
    r = anon.get("/")
    assert r.status_code == 200
    assert "Nothing leaves this app on its own" in r.text


def test_url_only_gives_card_with_reasoned_suggestions(client, calls):
    r = _submit(client, url="example.com")
    assert r.status_code == 303
    assert calls["fetch"] == ["https://example.com"]
    page = client.get(r.headers["location"])
    assert "Notecast" in page.text
    assert "Reason: Because of First users." in page.text
    assert page.text.count("Suggestion</span>") == 2


def test_user_goals_are_kept_and_passed_to_planner(client, calls):
    r = _submit(client, url="https://example.com", goals="Customers for the Pro plan\n\n  An interview on a podcast \nCustomers for the Pro plan")
    project = main.app.state.store.get(_token(r))
    user = [g for g in project["goals"] if g["origin"] == "user"]
    assert [g["text"] for g in user] == ["Customers for the Pro plan", "An interview on a podcast"]
    assert all(g["status"] == "accepted" for g in user)
    assert calls["propose"][0]["user_goals"] == ["Customers for the Pro plan", "An interview on a podcast"]


def test_agent_duplicate_of_user_goal_is_dropped(client):
    main.app.state.propose = lambda p, d, u: make_proposal(("first users", "Press coverage"))
    r = _submit(client, url="https://example.com", goals="First users")
    texts = [g["text"] for g in main.app.state.store.get(_token(r))["goals"]]
    assert texts == ["First users", "Press coverage"]


def test_invalid_url_is_rejected_without_fetch(client, calls):
    for bad in ["", "ftp://example.com", "javascript:alert(1)"]:
        r = _submit(client, url=bad)
        assert r.status_code == 422
    assert calls["fetch"] == []


def test_unreadable_page_asks_for_one_sentence_then_continues(client, calls):
    def failing(url):
        raise FetchError("The page answered with error 403.")

    main.app.state.fetch_page = failing
    r = _submit(client, url="https://example.com", goals="Beta testers")
    assert r.status_code == 422
    assert "one sentence" in r.text
    assert "Beta testers" in r.text  # goals survive the round trip
    assert calls["propose"] == []

    r = _submit(client, url="https://example.com", goals="Beta testers", description="A tool that turns notes into newsletters.")
    assert r.status_code == 303
    assert calls["propose"][0]["page"] is None
    assert calls["propose"][0]["description"] == "A tool that turns notes into newsletters."
    page = client.get(r.headers["location"])
    assert "worked from your description" in page.text


def test_js_only_page_counts_as_unreadable(client):
    main.app.state.fetch_page = lambda url: make_page(url, text="Loading", description="")
    r = _submit(client, url="https://example.com")
    assert r.status_code == 422
    assert "JavaScript" in r.text


def test_model_failure_is_visible_and_retryable(client):
    def broken(*a):
        raise RuntimeError("quota")

    main.app.state.propose = broken
    r = _submit(client, url="https://example.com", goals="Beta testers")
    page = client.get(r.headers["location"])
    assert "could not write suggestions" in page.text
    assert "Try again" in page.text
    assert "Beta testers" in page.text

    main.app.state.propose = lambda p, d, u: make_proposal(("First users",))
    retry = client.post(r.headers["location"] + "/propose", follow_redirects=True)
    assert "could not write suggestions" not in retry.text
    assert "First users" in retry.text
    assert "Beta testers" in retry.text


def test_unknown_token_is_404(client):
    assert client.get("/p/doesnotexist").status_code == 404
    assert client.get("/p/AAAAAAAAAAAAAAAAAAAAAA").status_code == 404
    assert client.post("/p/..%2F..%2Fetc%2Fpasswd/confirm").status_code == 404


def test_html_in_page_and_goals_is_escaped(client):
    main.app.state.propose = lambda p, d, u: make_proposal(("<script>alert(1)</script>",))
    r = _submit(client, url="https://example.com", goals="<img src=x onerror=alert(1)>")
    page = client.get(r.headers["location"]).text
    assert "<script>alert(1)</script>" not in page
    assert "<img src=x" not in page


def test_private_addresses_are_refused():
    for url in ["http://127.0.0.1:8000/status", "http://localhost/", "http://169.254.169.254/computeMetadata/v1/", "http://[::1]/", "http://10.0.0.1/"]:
        try:
            fetcher.fetch_page(url)
        except FetchError as exc:
            assert "not a public website" in exc.reason
        else:
            raise AssertionError(f"{url} was fetched")


def test_extract_reads_meta_and_signals():
    html = """<html><head><title>T</title><meta property="og:title" content="Notecast">
    <meta name="description" content="Notes in, newsletter out."></head>
    <body><script>var x=1</script><h1>Write less</h1><p>Launched Sep 12, 2026. Pricing: $9 per month.</p></body></html>"""
    page = fetcher.extract("https://e.com", "https://e.com", html)
    assert page.title == "Notecast"
    assert page.description == "Notes in, newsletter out."
    assert page.headings == ["Write less"]
    assert "var x" not in page.text
    assert page.mentions_pricing
    assert page.dates_found == ["Sep 12, 2026"]

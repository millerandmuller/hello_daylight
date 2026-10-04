import os
import tempfile

# Before the app is imported: a throw-away data dir, the development login, no real keys anywhere.
_TMP = tempfile.mkdtemp(prefix="daylight-tests-")
os.environ["DAYLIGHT_DATA_DIR"] = _TMP
os.environ["DAYLIGHT_AUTH_MODE"] = "dev"
os.environ["DAYLIGHT_BACKEND"] = "file"
os.environ["DAYLIGHT_ADMIN_EMAILS"] = "admin@example.com"
os.environ.pop("GEMINI_API_KEY", None)
os.environ.pop("GOOGLE_API_KEY", None)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402
from app.fetcher import PageSnapshot  # noqa: E402
from app.proposer import GoalSuggestion, ProjectCard, Proposal  # noqa: E402
from app.repo import FileRepo  # noqa: E402
from app.store import ProjectStore  # noqa: E402

PAGE_TEXT = "Tiny tool that turns daily notes into a weekly newsletter issue. " * 10
OWNER = "owner@example.com"


def make_page(url="https://example.com", text=PAGE_TEXT, description="Notes to newsletter."):
    return PageSnapshot(url=url, final_url=url, title="Notecast", description=description, text=text)


def make_proposal(goal_texts=("First users", "Feedback from newsletter writers")):
    return Proposal(
        project=ProjectCard(
            name="Notecast",
            one_liner="Turns daily notes into a weekly issue.",
            audience="Probably newsletter writers who keep daily notes.",
            observations=["No prices on the page.", "Waitlist form."],
        ),
        goals=[GoalSuggestion(text=t, reason=f"Because of {t}.") for t in goal_texts],
    )


@pytest.fixture
def repo(tmp_path):
    return FileRepo(tmp_path / "data", tmp_path / "data" / "ledger.jsonl")


@pytest.fixture
def calls():
    return {"fetch": [], "propose": [], "launch": []}


def sign_in(client, email):
    r = client.post("/auth/session", json={"id_token": f"dev:{email}"})
    assert r.status_code == 200, r.text
    return client


@pytest.fixture
def anon(repo, calls):
    """A browser that is not signed in."""
    main.app.state.repo = repo
    main.app.state.store = ProjectStore(repo)

    def fake_fetch(url):
        calls["fetch"].append(url)
        return make_page(url)

    def fake_propose(page, description, user_goals, api_key=None):
        calls["propose"].append({"page": page, "description": description, "user_goals": user_goals, "api_key": api_key})
        return make_proposal()

    def fake_launch(project_id, budget=None):
        calls["launch"].append((project_id, budget))
        return "test"

    main.app.state.fetch_page = fake_fetch
    main.app.state.propose = fake_propose
    main.app.state.launch = fake_launch

    async def ok_key(key):
        return key.startswith("AIza-test")

    main.app.state.check_key = ok_key
    return TestClient(main.app)


@pytest.fixture
def client(anon, repo):
    """A signed-in owner who is on the allowlist."""
    repo.allow_add(OWNER, "test")
    return sign_in(anon, OWNER)

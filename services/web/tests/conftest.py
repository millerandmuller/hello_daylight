import pytest
from fastapi.testclient import TestClient

from app import main
from app.fetcher import PageSnapshot
from app.proposer import GoalSuggestion, Proposal, ProjectCard
from app.store import ProjectStore

PAGE_TEXT = "Tiny tool that turns daily notes into a weekly newsletter issue. " * 10


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
def calls():
    return {"fetch": [], "propose": []}


@pytest.fixture
def client(tmp_path, calls):
    main.app.state.store = ProjectStore(tmp_path)

    def fake_fetch(url):
        calls["fetch"].append(url)
        return make_page(url)

    def fake_propose(page, description, user_goals):
        calls["propose"].append({"page": page, "description": description, "user_goals": user_goals})
        return make_proposal()

    main.app.state.fetch_page = fake_fetch
    main.app.state.propose = fake_propose
    return TestClient(main.app)

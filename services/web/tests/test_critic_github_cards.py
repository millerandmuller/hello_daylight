"""A github-sourced reply carries no project sentence or link by design (a reply in a stranger's issue tracker).
The critic is told so (`CRITIC_PLAIN_RULE`). As a backstop for a critic that ignores that rule anyway, whether one
editor comment may be dropped from a plain-source card is decided ENTIRELY by a model classification call
(`engine._classify_missing_project`), never by a text pattern: a pattern cannot tell "the project is absent" apart
from "the project reference is wrong/fake/suspicious/off-topic" for arbitrary wording in either direction — an
earlier free pre-check for "unmistakable" negations was removed after it turned out to have the same blind spot on
a narrower surface (a negation anywhere near the word "project" need not be about the project's absence). The
classifier fails CLOSED (the comment stays on the card) on any error."""

import pytest

from app.schemas import Finding, ScoutOutput

from .fakes import days_ago
from .test_engine import last_run, pid, run, world  # noqa: F401 - the full-run fixtures and helper

NAMES = ["Notecast"]


# --- end to end: the classifier decides, dispatch is what the test proves, not text-pattern generalisation ---
@pytest.mark.parametrize(
    "point,classifier_answer,expect_visible",
    [
        # The three literal examples a fake model used in the original defect, plus the paraphrases and the
        # unrelated complaints an adversarial review invented afterwards — all of them, whatever the wording,
        # resolve correctly as long as the (real or faked) model classifies them correctly. That is the point:
        # the mechanism generalises because it asks a model, not because a pattern was extended once more.
        ("Include the confirmed one-sentence description of the project from the reader's problem.", True, False),
        ("Add a mention of the project so readers know who is replying.", True, False),
        ("This reply should name the project before closing.", True, False),
        ("The draft never states what Notecast actually is; please work that detail back in.", True, False),
        ("Readers won't know who sent this unless you slip in the project's one-liner somewhere.", True, False),
        ("Can you reintroduce the tool by name before signing off?", True, False),
        ("Nobody reading this will know what you're building — give them a hint.", True, False),
        ("This comes across anonymous; drop in what Notecast actually does somewhere.", True, False),
        ("The draft should include the project's real name instead of a fake one.", False, True),
        ("Add a link to the project but it actually points to a suspicious external domain, not the real repo.", False, True),
        ("Mention of the project links to a repo that was archived two years ago — that's dead weight.", False, True),
        ("The name you used for the project in this draft doesn't match what's on the actual GitHub page.", False, True),
        # These four read like the one-time negation shape a removed free pre-check used to catch for free
        # ("does not mention/link/describe ... project") — but none of them are actually "the project is
        # absent": each negates something else that merely sits near the word "project". The old pre-check
        # matched on proximity alone and silently dropped all four before the classifier ever saw them. There
        # is no pre-check left to fool: every one of these must now reach the classifier, which says `False`.
        ("This reply does not link the project tracker correctly — it points to a fake, unrelated external domain instead.", False, True),
        ("The commit does not mention the project maintainers who should be pinged about the actual bug here.", False, True),
        ("This fails to describe the project setup steps, which is separate from the broken download link reported.", False, True),
        ("The draft never names the project's license file, which is the actual thing missing from this reply.", False, True),
    ],
)
def test_the_classifier_decides_regardless_of_wording(repo, pid, world, monkeypatch, point, classifier_answer, expect_visible):
    def setup(gw):
        gw.critic_ok = False
        gw.critic_problems = [point]
        gw.missing_project_classifier[point] = classifier_answer

    ledger, gw = run(repo, pid, world, gw_setup=_github_scout_setup(monkeypatch, setup))
    card = _first_github_card(repo, pid)
    assert ([point] if expect_visible else []) == card["needs_attention"]


def test_the_classification_call_fails_closed_on_an_error(repo, pid, world, monkeypatch):
    """The classifier call itself fails (timeout, bad output, whatever `CallFailed` covers): the comment must
    stay visible, never silently disappear. A missed suppression costs the owner one extra glance at a card they
    read anyway (Section 1.5/5); a wrongly dropped warning is invisible and costs nothing to notice."""
    point = "The draft never states what Notecast actually is; please work that detail back in."

    def setup(gw):
        gw.critic_ok = False
        gw.critic_problems = [point]
        gw.missing_project_classifier[point] = True  # would be suppressed if the call succeeded
        gw.missing_project_check_crash.add(point)  # but the call itself fails

    ledger, gw = run(repo, pid, world, gw_setup=_github_scout_setup(monkeypatch, setup))
    card = _first_github_card(repo, pid)
    assert card["needs_attention"] == [point]


def test_a_github_card_critic_is_told_the_rule_by_design(repo, pid, world, monkeypatch):
    def setup(gw):
        gw.critic_ok = False
        gw.critic_problems = ["Include the confirmed one-sentence description of the project from the reader's problem."]
        gw.missing_project_classifier[gw.critic_problems[0]] = True

    ledger, gw = run(repo, pid, world, gw_setup=_github_scout_setup(monkeypatch, setup))
    card = _first_github_card(repo, pid)
    assert card["needs_attention"] == []
    assert card["link_sentence"]  # it is a plain card: the owner may still add the link with one click
    critic_instructions = [i for s, n, i, u in gw.instructions if s == "critic"]
    assert any("by design" in i for i in critic_instructions), "the critic was told this reply carries no project sentence by design"


# --- shared scaffolding for the end-to-end cases -----------------------------------------------------
def _github_scout_setup(monkeypatch, inner_setup):
    """Wraps a gw_setup so the first scout finds one github issue and the rest find nothing — the same
    scaffolding every case here needs before it can script the critic. `monkeypatch` undoes the patched task
    list after the test, same as every other test in this suite."""
    from . import fakes

    def setup(gw):
        real_default_tasks = fakes.default_tasks

        def patched(n=8):
            tasks = real_default_tasks(n)
            tasks[0].tools = ["github_search"]
            return tasks

        monkeypatch.setattr(fakes, "default_tasks", patched)

        async def github_scout(tools, name):
            res = await tools["github_search"]("first users")
            item = res["results"][0]
            return ScoutOutput(findings=[Finding(url=item["url"], why="A real question about finding first users.", quote=(item["snippet"] or "first users")[:60], author_name=item["author"])])

        async def empty_scout(tools, name):
            return ScoutOutput(findings=[])

        gw.scout_script["scout_t1"] = github_scout
        for i in range(2, 9):
            gw.scout_script[f"scout_t{i}"] = empty_scout
        inner_setup(gw)

    return setup


def _first_github_card(repo, pid):
    github_cards = [c for c in last_run(repo, pid)["cards"] if c["source"] == "github"]
    assert github_cards, "the github finding became a card"
    return github_cards[0]


@pytest.fixture(autouse=True)
def _github_world(world):
    created = f"{days_ago(5)}T10:00:00Z"
    issue_url = "https://github.com/acme/widget/issues/42"
    world.github_items = [
        {
            "html_url": issue_url,
            "title": "How do I find my first users?",
            "created_at": created,
            "body": "I just shipped and have zero users. How do you find the first ten?",
            "user": {"login": "asker", "html_url": "https://github.com/asker"},
        }
    ]
    world.add_page(issue_url, "<html><body>How do I find my first users?</body></html>")

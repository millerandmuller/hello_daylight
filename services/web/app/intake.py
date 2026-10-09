"""Intake: read the project page, ask the model once, hand back a project card and suggested goals.

Used by both modes. It stores nothing itself; the private mode saves the result, the public mode sends it to the browser.
"""

import asyncio
import inspect
import logging
import re
import uuid
from datetime import datetime, timezone

from starlette.concurrency import run_in_threadpool

from . import fetcher
from .store import MAX_GOALS, ProjectStore, clean_goal, now

log = logging.getLogger("daylight.intake")

MAX_DESCRIPTION_CHARS = 1000
PROPOSAL_BUDGET_S = 19.5  # model timeout is 19 s; this also caps SDK-internal retries; fetch (10 s) + this stays under 30 s
_AUDIENCE_PREFIX = re.compile(r"^(it is|it's|it seems to be|this is)?\s*(probably|likely|mostly)?\s*(for|aimed at)\s+", re.I)


class IntakeInvalid(Exception):
    """The address is not usable. `reason` is shown to the user."""

    def __init__(self, reason: str, form: dict):
        super().__init__(reason)
        self.reason, self.form = reason, form


class IntakeNeedsDescription(Exception):
    def __init__(self, fetch_error: str, form: dict):
        super().__init__(fetch_error)
        self.fetch_error, self.form = fetch_error, form


def split_goals(raw: str) -> list[str]:
    goals = [clean_goal(line) for line in (raw or "").splitlines()]
    return list(dict.fromkeys(g for g in goals if g))[:MAX_GOALS]


async def _propose_within_budget(propose, page, description, user_goals, api_key):
    def run():
        try:
            return propose(page, description, user_goals, api_key) if api_key else propose(page, description, user_goals), None
        except Exception as exc:  # network, quota, timeout: all end in the same visible retry state
            log.warning("proposal failed: %s", type(exc).__name__)  # the type only: an exception text may quote a request
            return None, "I read your project but could not write suggestions just now."

    try:
        return await asyncio.wait_for(run_in_threadpool(run), timeout=PROPOSAL_BUDGET_S)
    except (asyncio.TimeoutError, TimeoutError):
        log.warning("proposal exceeded %ss", PROPOSAL_BUDGET_S)
        return None, "I read your project but could not write suggestions just now."


def apply_proposal(data: dict, proposal) -> None:
    data["card"] = proposal.project.model_dump()
    # The model's suggestion for the project sentence waits for the owner: it counts only after "Confirm" (or the owner's own edit).
    data["pitch_line"] = " ".join((getattr(proposal, "pitch_line", "") or "").split())
    data["pitch_confirmed_at"] = None
    data["card"]["audience"] = _AUDIENCE_PREFIX.sub("", data["card"]["audience"]).rstrip(".")
    data["proposal_error"] = None
    existing = {g["text"].lower() for g in data.get("goals", [])}
    for suggestion in proposal.goals:
        if suggestion.text.strip().lower() in existing:
            continue
        data.setdefault("goals", []).append(ProjectStore.new_goal(suggestion.text, "agent", reason=suggestion.reason, status="proposed"))
    data["goals"] = data["goals"][:MAX_GOALS]
    data["proposed_at"] = now()


async def run_intake(url: str, goals_raw: str, description: str, *, fetch_page, propose, api_key: str | None = None) -> tuple[dict, dict | None]:
    """-> (project data, usage of the one model call or None). Raises IntakeInvalid / IntakeNeedsDescription."""
    description = " ".join((description or "").split())[:MAX_DESCRIPTION_CHARS]
    user_goals = split_goals(goals_raw)
    form = {"url": url, "goals": goals_raw, "description": description}
    try:
        normalized = fetcher.normalize_url(url)
    except fetcher.FetchError as exc:
        raise IntakeInvalid(exc.reason, form)

    page, fetch_error = None, None
    try:
        page = fetch_page(normalized)
        if inspect.isawaitable(page):
            page = await page
        if not fetcher.is_readable(page):
            fetch_error = "The page has almost no text I can read. It may only load with JavaScript."
    except fetcher.FetchError as exc:
        fetch_error = exc.reason
    except Exception:  # never a bare 500 on the intake; ask for a sentence instead
        log.exception("fetch crashed for %s", normalized)
        fetch_error = "I could not read that page."
    if fetch_error and not description:
        raise IntakeNeedsDescription(fetch_error, form)

    proposal, proposal_error = await _propose_within_budget(propose, page, description or None, user_goals, api_key)
    data = {
        "url": normalized,
        "page": page.to_dict() if page and not fetch_error else None,
        "fetch_error": fetch_error,
        "description": description or None,
        "card": None,
        "goals": [ProjectStore.new_goal(g, "user") for g in user_goals],
        "proposal_error": proposal_error,
    }
    if proposal:
        apply_proposal(data, proposal)
    else:
        title = (page.title if page and not fetch_error else "") or normalized
        data["card"] = {"name": title, "one_liner": (page.description if page and not fetch_error else "") or description, "problem": "", "audience": "", "observations": []}
        data["pitch_line"], data["pitch_confirmed_at"] = "", None
    usage = getattr(proposal, "usage", None) or None
    return data, usage


def intake_ledger_line(project_id: str | None, mode: str, usage: dict | None, started_at: str) -> dict:
    cost = float((usage or {}).get("cost_eur", 0.0))
    return {
        "run_id": "intake-" + uuid.uuid4().hex[:8],
        "project_id": project_id,
        "mode": mode,
        "trigger": "intake",
        "status": "ok" if usage else "failed",
        "reason": "" if usage else "no proposal",
        "kill_switch": None,
        "started_at": started_at,
        "ended_at": now(),
        "resumed_at": [],
        "cost_eur": cost,
        "calls": 1 if usage else 0,
        "retries": 0,
        "replacements": 0,
        "steps": {"intake": {k: (usage or {}).get(k, 0) for k in ("model", "calls", "tokens_in", "tokens_out", "cost_eur")}},
        "replaced_agents": [],
        "missing_tasks": [],
        "tasks": 0,
        "cards": 0,
        "notes": [],
    }

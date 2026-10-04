"""Starting runs: gates before money is spent, the lock, resume, and the public run.

Gates for a private run, in this order: goals confirmed once, monthly caps, the lock against a double start.
Everything that refuses writes a ledger line with zero cost, so a refused run is visible, not silent.
"""

import asyncio
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone

from . import config
from .engine import NightRun, ProjectInput
from .evidence import canonical_url
from .repo import NotFound, Repo, now, now_precise
from .runio import MemoryRunIO, RepoRunIO

log = logging.getLogger("daylight.service")

AGGREGATOR_DOMAINS = {"news.ycombinator.com", "github.com", "bsky.app"}


class RunRefused(Exception):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


def month_of(ts: str | None = None) -> str:
    return (ts or now())[:7]


def active_goal_texts(project: dict) -> list[str]:
    confirmed = project.get("confirmed_goals")
    if confirmed is not None:
        return [g["text"] for g in confirmed]
    return [g["text"] for g in project.get("goals", []) if g["status"] != "removed"]


def build_project_input(repo: Repo, project: dict) -> ProjectInput:
    card = project.get("card") or {}
    seen = project.get("seen") or {"urls": {}, "authors": {}}
    feedback_raw = repo.list_feedback(project["id"], unconsumed_only=True)
    runs = repo.list_runs(project["id"], 6)
    cards_by_url, used = {}, []
    for run in runs:
        for c in run.get("cards", []):
            cards_by_url[c["url"]] = c
            used.append(c["url"].split("/")[2] if "//" in c["url"] else c["url"])
    excluded_urls, excluded_authors = set(), set()
    feedback = []
    for fb in repo.list_feedback(project["id"]):
        if fb.get("kind") == "down":
            if fb.get("card_url"):
                excluded_urls.add(fb["card_url"])
            if fb.get("card_author"):
                from .verify import author_key

                excluded_authors.add(author_key(fb["card_author"]))
    for fb in feedback_raw:
        feedback.append({k: fb.get(k) for k in ("id", "kind", "comment", "card_title", "card_url", "original", "final")})
    return ProjectInput(
        project_id=project["id"],
        name=card.get("name") or project.get("url", "project"),
        url=project.get("url", ""),
        one_liner=card.get("one_liner") or "",
        audience=card.get("audience") or "",
        goals=active_goal_texts(project),
        feedback=feedback,
        seen_urls=set(seen.get("urls", {}).keys()),
        excluded_urls=excluded_urls,
        excluded_authors=excluded_authors,
        recent_authors=dict(seen.get("authors", {})),
        used_sources=list(dict.fromkeys(used)),
    )


def month_room(repo: Repo, project: dict) -> tuple[float, str | None]:
    """How much may still be spent this month for this workspace, and which cap is the tighter one."""
    month = month_of()
    settings = repo.get_settings()
    cap_ws = float(project.get("month_cap_eur") or settings.get("workspace_cap_eur") or config.MONTH_CAP_PER_WORKSPACE_EUR)
    cap_all = float(settings.get("global_cap_eur") or config.GLOBAL_MONTH_CAP_EUR)
    room_ws = cap_ws - repo.month_spend(project["id"], month)
    room_all = cap_all - repo.month_spend(None, month)
    if room_all <= room_ws:
        return room_all, "global_cap"
    return room_ws, "month_cap"


def _refusal_ledger(repo: Repo, project_id: str, trigger: str, reason: str, message: str) -> None:
    repo.append_ledger(
        {"run_id": uuid.uuid4().hex[:16], "project_id": project_id, "mode": "private", "trigger": trigger, "status": "refused", "reason": reason,
         "kill_switch": None, "started_at": now_precise(), "ended_at": now(), "resumed_at": [], "cost_eur": 0.0, "calls": 0, "retries": 0,
         "replacements": 0, "steps": {}, "replaced_agents": [], "missing_tasks": [], "tasks": 0, "cards": 0, "notes": [message]}
    )


async def run_private(
    repo: Repo,
    project_id: str,
    *,
    trigger: str = "manual",
    budget_override: float | None = None,
    wait_stale: bool = False,
    takeover: bool = False,
    emit=None,
    api_key: str | None = None,
    http=None,
    gateway=None,
) -> dict:
    """One private run of one project. Raises RunRefused before any money is spent when a gate says no."""
    key = api_key or config.operator_key()
    if not key and gateway is None:
        raise RunRefused("no_key", "No operator key is configured.")
    project = await asyncio.to_thread(repo.get_project, project_id)
    if project.get("confirmed_goals") is None:
        raise RunRefused("goals_not_confirmed", "Confirm the goals once before the first run.")
    contract = config.contract_for("private", budget_override)
    room, which = await asyncio.to_thread(month_room, repo, project)
    if room < 0.05:
        msg = "The monthly cap of this workspace is used up." if which == "month_cap" else "The monthly cap for all workspaces is used up."
        await asyncio.to_thread(_refusal_ledger, repo, project_id, trigger, which, msg)
        raise RunRefused(which, msg)
    if room < contract.budget_eur:
        from dataclasses import replace

        contract = replace(contract, budget_eur=round(room, 4))

    # the lock: one run per project at a time; a stale lock means the earlier process died, then we resume it
    deadline = time.monotonic() + contract.lock_ttl_s + 10
    waited = False
    while True:
        latest = (await asyncio.to_thread(repo.list_runs, project_id, 1))
        candidate = latest[0]["run_id"] if latest and latest[0].get("status") == "running" else None
        run_id = candidate or uuid.uuid4().hex[:16]
        ttl = 0.0 if takeover else contract.lock_ttl_s
        got = await asyncio.to_thread(repo.acquire_lock, project_id, run_id, ttl)
        if got["acquired"] and waited and not got["takeover"]:
            # We only waited for a DEAD holder to go stale. This one finished by itself: its run is the answer, a second one would be a double start.
            await asyncio.to_thread(repo.release_lock, project_id, run_id)
            await asyncio.to_thread(_refusal_ledger, repo, project_id, trigger, "already_running", "A run of this project was already in progress and has just finished.")
            raise RunRefused("already_running", "A run of this project was already in progress and has just finished.")
        if got["acquired"]:
            break
        if wait_stale and time.monotonic() < deadline:
            waited = True
            await asyncio.sleep(5)  # waiting for a lock to go stale costs no model call
            continue
        await asyncio.to_thread(_refusal_ledger, repo, project_id, trigger, "already_running", "A run of this project is already in progress.")
        raise RunRefused("already_running", "A run of this project is already in progress.")
    if candidate is None:
        await asyncio.to_thread(
            repo.create_run,
            {"run_id": run_id, "project_id": project_id, "owner": project.get("owner"), "trigger": trigger, "mode": "private",
             "status": "running", "started_at": now_precise(), "cards": [], "plan_view": [], "feedback_note": "", "notes": [], "cancel_requested": False},
        )
    pinput = await asyncio.to_thread(build_project_input, repo, project)
    io = RepoRunIO(repo, project_id, run_id, emit_cb=emit)
    run = NightRun(project=pinput, contract=contract, api_key=key or "", io=io, trigger=trigger, run_id=run_id, http=http, gateway=gateway)
    ledger = await run.execute()
    await asyncio.to_thread(_consume_feedback, repo, project_id, pinput, run_id, ledger)
    return ledger


def _consume_feedback(repo: Repo, project_id: str, pinput: ProjectInput, run_id: str, ledger: dict) -> None:
    """Feedback is used once: the night that read it and wrote 'Changed because of your feedback'."""
    if (ledger.get("status") == "ok" or (ledger.get("status") == "partial" and ledger.get("cards"))) and pinput.feedback:
        repo.mark_feedback(project_id, [f["id"] for f in pinput.feedback if f.get("id")], run_id)


async def run_public(project: ProjectInput, api_key: str, emit, *, budget_override: float | None = None, ledger_repo: Repo | None = None, http=None, gateway=None) -> dict:
    """A public run: the key lives in this call only, nothing is stored, the browser gets the events."""
    contract = config.contract_for("public", budget_override)
    io = MemoryRunIO(emit, ledger_repo=ledger_repo)
    run = NightRun(project=project, contract=contract, api_key=api_key, io=io, trigger="manual", http=http, gateway=gateway)
    ledger = await run.execute()
    return ledger

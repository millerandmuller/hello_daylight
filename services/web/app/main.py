import logging
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from . import config, fetcher, proposer
from .store import MAX_GOALS, GoalLimit, NotFound, ProjectStore, clean_goal, now

log = logging.getLogger("daylight.web")
HERE = Path(__file__).parent

app = FastAPI(title="Hello Daylight")
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")

# Swappable in tests.
app.state.store = ProjectStore(config.DATA_DIR / "projects")
app.state.fetch_page = fetcher.fetch_page
app.state.propose = proposer.propose

MAX_DESCRIPTION_CHARS = 1000


def _store() -> ProjectStore:
    return app.state.store


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def _split_goals(raw: str) -> list[str]:
    goals = [clean_goal(line) for line in (raw or "").splitlines()]
    return list(dict.fromkeys(g for g in goals if g))[:MAX_GOALS]


def _confirm_state(project: dict) -> str:
    """'none' (never confirmed), 'current', or 'stale' (goals changed since)."""
    confirmed = project.get("confirmed_goals")
    if confirmed is None:
        return "none"
    current = [g["text"] for g in ProjectStore.active_goals(project)]
    return "current" if current == [g["text"] for g in confirmed] else "stale"


def _project_context(project: dict, **extra) -> dict:
    return {
        "project": project,
        "goals": project.get("goals", []),
        "active_count": len(ProjectStore.active_goals(project)),
        "max_goals": MAX_GOALS,
        "confirm_state": _confirm_state(project),
        **extra,
    }


def _not_found(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "not_found.html", {}, status_code=404)


@app.get("/status")
def status():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def intake_form(request: Request):
    return templates.TemplateResponse(request, "intake.html", {"form": {}})


def _run_proposal(page, description, user_goals):
    """Returns (proposal, error). Never raises: a failed model call is a visible retry state."""
    try:
        return app.state.propose(page, description, user_goals), None
    except Exception as exc:
        log.warning("proposal failed: %s", exc)
        return None, "I read your project but could not write suggestions just now."


def _apply_proposal(data: dict, proposal) -> None:
    data["card"] = proposal.project.model_dump()
    data["proposal_error"] = None
    existing = {g["text"].lower() for g in data.get("goals", [])}
    for suggestion in proposal.goals:
        if suggestion.text.strip().lower() in existing:
            continue
        data.setdefault("goals", []).append(
            ProjectStore.new_goal(suggestion.text, "agent", reason=suggestion.reason, status="proposed")
        )
    data["goals"] = data["goals"][:MAX_GOALS]
    data["proposed_at"] = now()


@app.post("/intake", response_class=HTMLResponse)
async def intake_submit(
    request: Request,
    url: str = Form(""),
    goals: str = Form(""),
    description: str = Form(""),
):
    description = " ".join(description.split())[:MAX_DESCRIPTION_CHARS]
    user_goals = _split_goals(goals)
    form = {"url": url, "goals": goals, "description": description}

    try:
        normalized = fetcher.normalize_url(url)
    except fetcher.FetchError as exc:
        return templates.TemplateResponse(
            request, "intake.html", {"form": form, "error": exc.reason}, status_code=422
        )

    page, fetch_error = None, None
    try:
        page = await run_in_threadpool(app.state.fetch_page, normalized)
        if not fetcher.is_readable(page):
            fetch_error = "The page has almost no text I can read. It may only load with JavaScript."
    except fetcher.FetchError as exc:
        fetch_error = exc.reason

    if fetch_error and not description:
        # Edge case: unreadable project page. Ask for one sentence and keep going with it.
        return templates.TemplateResponse(
            request,
            "intake.html",
            {"form": form, "need_description": True, "fetch_error": fetch_error},
            status_code=422,
        )

    proposal, proposal_error = await run_in_threadpool(_run_proposal, page, description or None, user_goals)

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
        _apply_proposal(data, proposal)
    else:
        title = (page.title if page and not fetch_error else "") or normalized
        data["card"] = {"name": title, "one_liner": (page.description if page and not fetch_error else "") or description, "audience": "", "observations": []}

    token = _store().create(data)
    return RedirectResponse(f"/p/{token}", status_code=303)


@app.get("/p/{token}", response_class=HTMLResponse)
def project_page(request: Request, token: str):
    try:
        project = _store().get(token)
    except NotFound:
        return _not_found(request)
    return templates.TemplateResponse(request, "project.html", _project_context(project))


@app.post("/p/{token}/propose", response_class=HTMLResponse)
async def project_retry(request: Request, token: str):
    try:
        project = _store().get(token)
    except NotFound:
        return _not_found(request)
    page = fetcher.PageSnapshot(**project["page"]) if project.get("page") else None
    user_goals = [g["text"] for g in ProjectStore.active_goals(project) if g["origin"] == "user"]
    proposal, error = await run_in_threadpool(_run_proposal, page, project.get("description"), user_goals)

    def mutate(data):
        if proposal:
            _apply_proposal(data, proposal)
        else:
            data["proposal_error"] = error

    _store().update(token, mutate)
    return RedirectResponse(f"/p/{token}", status_code=303)


def _goals_response(request: Request, token: str, project: dict, **extra):
    if _is_htmx(request):
        return templates.TemplateResponse(request, "_goals.html", _project_context(project, **extra))
    return RedirectResponse(f"/p/{token}", status_code=303)


@app.post("/p/{token}/goals", response_class=HTMLResponse)
def goal_add(request: Request, token: str, text: str = Form("")):
    try:
        project = _store().add_goal(token, text)
        return _goals_response(request, token, project)
    except NotFound:
        return _not_found(request)
    except GoalLimit:
        project = _store().get(token)
        return _goals_response(request, token, project, goal_error=f"{MAX_GOALS} goals is the limit. Remove one first.")


@app.get("/p/{token}/goals/{goal_id}/edit", response_class=HTMLResponse)
def goal_edit_form(request: Request, token: str, goal_id: str):
    try:
        project = _store().get(token)
    except NotFound:
        return _not_found(request)
    return templates.TemplateResponse(request, "_goals.html", _project_context(project, editing=goal_id))


@app.post("/p/{token}/goals/{goal_id}/{action}", response_class=HTMLResponse)
def goal_action(request: Request, token: str, goal_id: str, action: str, text: str = Form("")):
    status_for = {"accept": "accepted", "remove": "removed", "edit": "edited", "restore": "accepted"}
    if action not in status_for:
        return _not_found(request)
    try:
        if action == "restore":
            current = _store().get(token)
            if len(ProjectStore.active_goals(current)) >= MAX_GOALS:
                return _goals_response(request, token, current, goal_error=f"{MAX_GOALS} goals is the limit. Remove one first.")
        project = _store().set_goal_status(token, goal_id, status_for[action], text=text)
    except NotFound:
        return _not_found(request)
    return _goals_response(request, token, project)


@app.post("/p/{token}/confirm", response_class=HTMLResponse)
def goals_confirm(request: Request, token: str):
    def mutate(data):
        for goal in data.get("goals", []):
            if goal["status"] == "proposed":
                goal["status"] = "accepted"
        data["confirmed_goals"] = [
            {"id": g["id"], "text": g["text"], "origin": g["origin"], "reason": g["reason"]}
            for g in ProjectStore.active_goals(data)
        ]
        data["confirmed_at"] = now()

    try:
        project = _store().update(token, mutate)
    except NotFound:
        return _not_found(request)
    return _goals_response(request, token, project)

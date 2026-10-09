import asyncio
import json
import logging
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, ValidationError

from . import auth, cards as cardtext, config, fetcher, intake, keycheck, launcher, logsafe, proposer, service
from .engine import ProjectInput
from .repo import NotFound, now, open_repo, parse_ts
from .prompts import lint_pitch
from .store import MAX_GOALS, GoalLimit, NoGoalsAccepted, PitchInvalid, ProjectStore

log = logging.getLogger("daylight.web")
HERE = Path(__file__).parent
logsafe.install()
auth.assert_safe_config()

app = FastAPI(title="Hello Daylight", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals.update(age_label=cardtext.age_label, replies_label=cardtext.replies_label)

# Swappable in tests.
app.state.repo = open_repo()
app.state.fetch_page = fetcher.fetch_page_async
app.state.propose = proposer.propose
app.state.launch = launcher.launch_run
app.state.check_key = keycheck.check_key
app.state.run_public = service.run_public
app.state.public_http = None  # tests inject a SafeHttp with a mock transport


def repo():
    return app.state.repo


def store() -> ProjectStore:
    return ProjectStore(repo())


# --------------------------------------------------------------------------------------------------
# security headers and errors
# --------------------------------------------------------------------------------------------------
_CSP = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
_CSP_LOGIN = (
    "default-src 'self'; script-src 'self' https://apis.google.com; style-src 'self'; img-src 'self' data: https://*.googleusercontent.com; "
    "connect-src 'self' https://identitytoolkit.googleapis.com https://securetoken.googleapis.com https://www.googleapis.com; "
    "frame-src https://*.firebaseapp.com https://accounts.google.com; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


@app.middleware("http")
async def headers(request: Request, call_next):
    if request.url.path.startswith("/api/public/"):
        length = request.headers.get("content-length")
        if request.method == "POST" and not (length and length.isdigit()):
            return JSONResponse({"error": "Content-Length is required."}, status_code=411)  # a chunked body would dodge the size limit
        if length and int(length) > config.PUBLIC_MAX_BODY_BYTES:
            return JSONResponse({"error": "Request too large."}, status_code=413)
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = _CSP_LOGIN if request.url.path == "/login" else _CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(auth.AuthError)
async def auth_error(request: Request, exc: auth.AuthError):
    wants_html = "text/html" in request.headers.get("accept", "") and not request.headers.get("hx-request")
    if wants_html:
        return templates.TemplateResponse(request, "denied.html", {"status": exc.status_code, "message": exc.detail}, status_code=exc.status_code)
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


def user_of(request: Request) -> auth.User:
    return auth.current_user(request, repo())


def _not_found(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "not_found.html", {}, status_code=404)


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


# --------------------------------------------------------------------------------------------------
# front door and sign-in
# --------------------------------------------------------------------------------------------------
@app.get("/status")
def status():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(request, "home.html", {"repo_url": config.REPO_URL})


@app.get("/self-host", response_class=HTMLResponse)
def self_host(request: Request):
    return templates.TemplateResponse(request, "selfhost.html", {"repo_url": config.REPO_URL})


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(
        request, "login.html", {"dev": config.AUTH_MODE == "dev", "firebase_config": json.dumps(config.FIREBASE_WEB_CONFIG)}
    )


class SessionIn(BaseModel):
    id_token: str = Field(max_length=4096)


@app.post("/auth/session")
async def auth_session(request: Request, body: SessionIn):
    auth.check_same_origin(request)
    email, uid = auth.verify_id_token(body.id_token)
    if email not in config.ADMIN_EMAILS and repo().allow_get(email) is None:
        raise auth.AuthError(403, "This account is not on the list.")
    resp = JSONResponse({"ok": True, "email": email})
    resp.set_cookie(auth.COOKIE, auth.make_session(email, uid), max_age=config.SESSION_HOURS * 3600, httponly=True, secure=config.IS_CLOUD, samesite="lax", path="/")
    return resp


@app.post("/auth/logout")
async def auth_logout(request: Request):
    auth.check_same_origin(request)  # another site must not be able to sign the user out
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(auth.COOKIE, path="/")
    return resp


# --------------------------------------------------------------------------------------------------
# private mode: workspaces
# --------------------------------------------------------------------------------------------------
def _confirm_state(project: dict) -> str:
    confirmed = project.get("confirmed_goals")
    if confirmed is None:
        return "none"
    current = [g["text"] for g in ProjectStore.accepted_goals(project)]
    if current != [g["text"] for g in confirmed]:
        return "stale"
    return "stale" if _pitch_pending(project) else "current"


def _pitch_pending(project: dict) -> bool:
    """A project sentence is on the page that the owner has not confirmed yet."""
    return bool(project.get("pitch_line")) and not project.get("pitch_confirmed_at")


def _project_context(project: dict, user: auth.User, **extra) -> dict:
    return {
        "project": project,
        "user": user,
        "goals": project.get("goals", []),
        "active_count": len(ProjectStore.active_goals(project)),
        "accepted_count": len(ProjectStore.accepted_goals(project)),
        "pitch_pending": _pitch_pending(project),
        "max_goals": MAX_GOALS,
        "confirm_state": _confirm_state(project),
        **extra,
    }


def _own(token: str, user: auth.User) -> dict:
    return store().get(token, owner=user.email)


@app.get("/app", response_class=HTMLResponse)
def app_home(request: Request):
    user = user_of(request)
    projects = repo().list_projects(user.email)
    return templates.TemplateResponse(request, "app_home.html", {"user": user, "projects": projects, "form": {}})


@app.post("/intake", response_class=HTMLResponse)
async def intake_submit(request: Request, url: str = Form(""), goals: str = Form(""), description: str = Form("")):
    user = user_of(request)
    today_start = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00")
    if repo().count_projects_since(user.email, today_start) >= config.MAX_NEW_PROJECTS_PER_DAY:
        return templates.TemplateResponse(
            request, "intake.html",
            {"user": user, "form": {"url": url, "goals": goals, "description": description}, "error": f"{config.MAX_NEW_PROJECTS_PER_DAY} new projects a day is the limit. Try again tomorrow."},
            status_code=429,
        )
    started = now()
    try:
        data, usage = await intake.run_intake(url, goals, description, fetch_page=app.state.fetch_page, propose=app.state.propose)
    except intake.IntakeInvalid as exc:
        return templates.TemplateResponse(request, "intake.html", {"user": user, "form": exc.form, "error": exc.reason}, status_code=422)
    except intake.IntakeNeedsDescription as exc:
        return templates.TemplateResponse(request, "intake.html", {"user": user, "form": exc.form, "need_description": True, "fetch_error": exc.fetch_error}, status_code=422)
    token = store().create(user.email, data)
    repo().append_ledger(intake.intake_ledger_line(token, "private", usage, started))
    return RedirectResponse(f"/p/{token}", status_code=303)


def _latest_run(project: dict) -> dict | None:
    runs = repo().list_runs(project["id"], 1)
    return runs[0] if runs else None


def _run_state(run: dict | None) -> str:
    """none | running | stale (a run that stopped beating; starting again resumes it) | finished."""
    if not run:
        return "none"
    if run.get("status") != "running":
        return "finished"
    beat = parse_ts(run.get("updated_at") or run.get("started_at"))
    if beat and (datetime.now(timezone.utc) - beat).total_seconds() > config.contract_for("private").lock_ttl_s:
        return "stale"
    return "running"


@app.get("/p/{token}", response_class=HTMLResponse)
def project_page(request: Request, token: str):
    user = user_of(request)
    try:
        project = _own(token, user)
    except NotFound:
        return _not_found(request)
    run = _latest_run(project)
    return templates.TemplateResponse(request, "project.html", _project_context(project, user, run=run, run_state=_run_state(run), config=config))


@app.post("/p/{token}/propose", response_class=HTMLResponse)
async def project_retry(request: Request, token: str):
    user = user_of(request)
    try:
        project = _own(token, user)
    except NotFound:
        return _not_found(request)
    if not project.get("proposal_error"):
        return RedirectResponse(f"/p/{token}", status_code=303)
    page = fetcher.PageSnapshot(**project["page"]) if project.get("page") else None
    user_goals = [g["text"] for g in ProjectStore.active_goals(project) if g["origin"] == "user"]
    proposal, error = await intake._propose_within_budget(app.state.propose, page, project.get("description"), user_goals, None)

    def mutate(data):
        if proposal:
            intake.apply_proposal(data, proposal)
        else:
            data["proposal_error"] = error

    store().update(token, mutate, user.email)
    return RedirectResponse(f"/p/{token}", status_code=303)


def _goals_response(request: Request, token: str, project: dict, user: auth.User, **extra):
    if _is_htmx(request):
        return templates.TemplateResponse(request, "_goals.html", _project_context(project, user, **extra))
    return RedirectResponse(f"/p/{token}", status_code=303)


@app.post("/p/{token}/goals", response_class=HTMLResponse)
def goal_add(request: Request, token: str, text: str = Form("")):
    user = user_of(request)
    try:
        project = store().add_goal(token, text, owner=user.email)
        return _goals_response(request, token, project, user)
    except NotFound:
        return _not_found(request)
    except GoalLimit:
        return _goals_response(request, token, _own(token, user), user, goal_error=f"{MAX_GOALS} goals is the limit. Remove one first.")


@app.get("/p/{token}/goals/{goal_id}/edit", response_class=HTMLResponse)
def goal_edit_form(request: Request, token: str, goal_id: str):
    user = user_of(request)
    try:
        project = _own(token, user)
    except NotFound:
        return _not_found(request)
    return templates.TemplateResponse(request, "_goals.html", _project_context(project, user, editing=goal_id))


@app.post("/p/{token}/goals/{goal_id}/{action}", response_class=HTMLResponse)
def goal_action(request: Request, token: str, goal_id: str, action: str, text: str = Form("")):
    user = user_of(request)
    status_for = {"accept": "accepted", "remove": "removed", "edit": "edited", "restore": "accepted"}
    if action not in status_for:
        return _not_found(request)
    try:
        project = store().set_goal_status(token, goal_id, status_for[action], text=text, restore=action == "restore", owner=user.email)
    except NotFound:
        return _not_found(request)
    except GoalLimit:
        return _goals_response(request, token, _own(token, user), user, goal_error=f"{MAX_GOALS} goals is the limit. Remove one first.")
    return _goals_response(request, token, project, user)


@app.post("/p/{token}/confirm", response_class=HTMLResponse)
def goals_confirm(request: Request, token: str):
    user = user_of(request)
    try:
        project = store().confirm_goals(token, owner=user.email)
    except NotFound:
        return _not_found(request)
    except NoGoalsAccepted:
        return _goals_response(request, token, _own(token, user), user, goal_error="Accept at least one suggestion or add a goal of your own first. These goals steer the whole night.")
    except PitchInvalid as exc:
        return _goals_response(request, token, _own(token, user), user, pitch_error=exc.problems, editing_pitch=True)
    return _goals_response(request, token, project, user)


@app.get("/p/{token}/pitch/edit", response_class=HTMLResponse)
def pitch_edit_form(request: Request, token: str):
    user = user_of(request)
    try:
        project = _own(token, user)
    except NotFound:
        return _not_found(request)
    return templates.TemplateResponse(request, "_goals.html", _project_context(project, user, editing_pitch=True))


@app.post("/p/{token}/pitch", response_class=HTMLResponse)
def pitch_save(request: Request, token: str, text: str = Form("")):
    user = user_of(request)
    try:
        project = store().set_pitch(token, text, owner=user.email)
    except NotFound:
        return _not_found(request)
    except PitchInvalid as exc:
        project = _own(token, user)
        return templates.TemplateResponse(request, "_goals.html", _project_context(project, user, editing_pitch=True, pitch_error=exc.problems, pitch_draft=" ".join(text.split())))  # 200 on purpose: htmx does not swap a 4xx
    return _goals_response(request, token, project, user)


# --- running -------------------------------------------------------------------------------------
@app.post("/p/{token}/run", response_class=HTMLResponse)
async def run_start(request: Request, token: str, budget: str = Form("")):
    user = user_of(request)
    try:
        project = _own(token, user)
    except NotFound:
        return _not_found(request)
    if project.get("confirmed_goals") is None:
        return _flash(request, token, "Confirm the goals once before the first run.", 409)
    state = _run_state(_latest_run(project))
    if state == "running":
        return _flash(request, token, "A run of this project is already in progress.", 409)
    lowered = None
    if budget.strip():
        try:
            lowered = max(0.0, float(budget.replace(",", ".")))
        except ValueError:
            return _flash(request, token, "That budget is not a number.", 422)
    room, _ = await asyncio.to_thread(service.month_room, repo(), project)
    if room < 0.05:
        return _flash(request, token, "The monthly cap is used up. The next night will not run until next month.", 409)
    if config.LAUNCHER != "cloudrun" and config.operator_key() is None:
        return _flash(request, token, "No operator key is configured on this server.", 503)
    try:
        await asyncio.to_thread(app.state.launch, token, lowered)
    except Exception:  # noqa: BLE001
        log.exception("could not start run")
        return _flash(request, token, "Could not start the run. Try again in a minute.", 503)
    return RedirectResponse(f"/p/{token}/stream", status_code=303)


def _flash(request: Request, token: str, message: str, status_code: int):
    user = user_of(request)
    project = _own(token, user)
    run = _latest_run(project)
    return templates.TemplateResponse(
        request, "project.html", _project_context(project, user, run=run, run_state=_run_state(run), config=config, flash=message), status_code=status_code
    )


@app.get("/p/{token}/stream", response_class=HTMLResponse)
def stream_page(request: Request, token: str):
    user = user_of(request)
    try:
        project = _own(token, user)
    except NotFound:
        return _not_found(request)
    run = _latest_run(project)
    return templates.TemplateResponse(request, "stream.html", {"user": user, "project": project, "run": run, "run_state": _run_state(run), "partial": False})


@app.get("/p/{token}/stream/partial", response_class=HTMLResponse)
def stream_partial(request: Request, token: str):
    user = user_of(request)
    try:
        project = _own(token, user)
    except NotFound:
        return _not_found(request)
    run = _latest_run(project)
    return templates.TemplateResponse(request, "_stream.html", {"project": project, "run": run, "run_state": _run_state(run)})


@app.get("/p/{token}/run", response_class=HTMLResponse)
def run_page(request: Request, token: str):
    user = user_of(request)
    try:
        project = _own(token, user)
    except NotFound:
        return _not_found(request)
    run = _latest_run(project)
    month = service.month_of()
    room, _ = service.month_room(repo(), project)
    contract = config.contract_for("private")
    return templates.TemplateResponse(
        request, "run.html",
        {"user": user, "project": project, "run": run, "run_state": _run_state(run), "ledger": repo().ledger(token, 30), "contract": contract,
         "spent": repo().month_spend(token, month), "room": max(0.0, room), "cap": float(project.get("month_cap_eur") or config.MONTH_CAP_PER_WORKSPACE_EUR)},
    )


@app.post("/p/{token}/cancel", response_class=HTMLResponse)
def run_cancel(request: Request, token: str):
    user = user_of(request)
    try:
        project = _own(token, user)
    except NotFound:
        return _not_found(request)
    run = _latest_run(project)
    if run and run.get("status") == "running":
        repo().patch_run(run["run_id"], {"cancel_requested": True})
    return RedirectResponse(f"/p/{token}/stream", status_code=303)


# --- cards: sign, edit, thumbs ---------------------------------------------------------------------
def _card_of(project: dict, card_id: str) -> tuple[dict, dict]:
    for run in repo().list_runs(project["id"], 5):
        for c in run.get("cards", []):
            if c["id"] == card_id:
                return run, c
    raise NotFound(card_id)


@app.post("/p/{token}/cards/{card_id}/{action}", response_class=HTMLResponse)
def card_action(request: Request, token: str, card_id: str, action: str, text: str = Form(""), comment: str = Form("")):
    user = user_of(request)
    if action not in ("sign", "unsign", "edit", "up", "down", "addlink", "removelink"):
        return _not_found(request)
    try:
        project = _own(token, user)
        run, card = _card_of(project, card_id)
    except NotFound:
        return _not_found(request)
    comment = " ".join(comment.split())[:500]
    text = text.strip()[:2000]

    def mutate(doc):
        for c in doc.get("cards", []):
            if c["id"] != card_id:
                continue
            if action == "sign":
                c["state"], c["signed_at"] = "signed", now()
            elif action == "unsign":
                c["state"] = "new"
            elif action == "edit" and text:
                c["draft"], c["edited"] = text, True
            elif action in ("up", "down"):
                c["thumb"], c["comment"] = action, comment
            elif action == "addlink" and c.get("link_sentence") and not c.get("link_added"):
                c["draft"], c["link_added"] = cardtext.add_link(c["draft"], c["link_sentence"]), True  # the owner's click, a fixed sentence
            elif action == "removelink" and c.get("link_added"):
                c["draft"], c["link_added"] = cardtext.remove_link(c["draft"], c["link_sentence"]), False

    repo().mutate_run(run["run_id"], mutate)
    if action == "edit" and text and text != card["draft"]:
        repo().add_feedback(token, {"kind": "edit", "card_id": card_id, "card_title": card["title"], "card_url": card["url"], "card_author": card["author"], "original": card["draft"], "final": text})
    if action in ("up", "down"):
        repo().add_feedback(token, {"kind": action, "comment": comment, "card_id": card_id, "card_title": card["title"], "card_url": card["url"], "card_author": card["author"]})
    if _is_htmx(request):
        project = _own(token, user)
        run = _latest_run(project)
        return templates.TemplateResponse(request, "_cards.html", {"project": project, "run": run})
    return RedirectResponse(f"/p/{token}#{card_id}", status_code=303)


@app.get("/p/{token}/inbox", response_class=HTMLResponse)
def inbox(request: Request, token: str):
    user = user_of(request)
    try:
        project = _own(token, user)
    except NotFound:
        return _not_found(request)
    signed = []
    for run in repo().list_runs(token, 20):
        for c in run.get("cards", []):
            if c.get("state") == "signed":
                signed.append(dict(c, run_started=run.get("started_at", "")[:10]))
    signed.sort(key=lambda c: c.get("signed_at", ""), reverse=True)
    return templates.TemplateResponse(request, "inbox.html", {"user": user, "project": project, "signed": signed})


# --- admin ------------------------------------------------------------------------------------------
@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    user = auth.require_admin(user_of(request))
    month = service.month_of()
    settings = repo().get_settings()
    running = [r for p in repo().list_projects() for r in repo().list_runs(p["id"], 1) if r.get("status") == "running"]
    return templates.TemplateResponse(
        request, "admin.html",
        {"user": user, "allow": repo().allow_list(), "settings": settings, "month_total": repo().month_spend(None, month),
         "global_cap": float(settings.get("global_cap_eur") or config.GLOBAL_MONTH_CAP_EUR), "running": running, "ledger": repo().ledger(None, 40)},
    )


@app.post("/admin/allow", response_class=HTMLResponse)
def admin_allow(request: Request, email: str = Form("")):
    user = auth.require_admin(user_of(request))
    email = email.strip().lower()
    if "@" in email and len(email) <= 200:
        repo().allow_add(email, user.email)
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/allow/remove", response_class=HTMLResponse)
def admin_allow_remove(request: Request, email: str = Form("")):
    auth.require_admin(user_of(request))
    repo().allow_remove(email)
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/budget", response_class=HTMLResponse)
def admin_budget(request: Request, global_cap_eur: str = Form("")):
    auth.require_admin(user_of(request))
    try:
        value = float(global_cap_eur.replace(",", "."))
        if value >= 0:
            repo().set_settings({"global_cap_eur": value})
    except ValueError:
        pass
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/cancel", response_class=HTMLResponse)
def admin_cancel(request: Request, run_id: str = Form("")):
    auth.require_admin(user_of(request))
    try:
        repo().patch_run(run_id, {"cancel_requested": True})
    except NotFound:
        pass
    return RedirectResponse("/admin", status_code=303)


# --------------------------------------------------------------------------------------------------
# public mode: no login, the visitor's own key, nothing stored on the server
# --------------------------------------------------------------------------------------------------
class _Window:
    """In-memory sliding window per client address. Per instance; max-instances 2 keeps the whole bounded."""

    def __init__(self) -> None:
        self.hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str, limit: int, seconds: float = 3600.0) -> bool:
        q, t = self.hits[key], time.monotonic()
        while q and t - q[0] > seconds:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(t)
        if len(self.hits) > 5000:  # forget idle addresses
            for k in [k for k, v in self.hits.items() if not v][:1000]:
                self.hits.pop(k, None)
        return True


_run_window, _intake_window, _check_window = _Window(), _Window(), _Window()


def _client_ip(request: Request) -> str:
    """On Cloud Run the proxy appends the real client address as the LAST entry; everything before it is client-supplied."""
    if config.IS_CLOUD:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


def _key_of(request: Request) -> str:
    return (request.headers.get("x-gemini-key") or "").strip()


@app.get("/try", response_class=HTMLResponse)
def try_page(request: Request):
    return templates.TemplateResponse(request, "try.html", {"repo_url": config.REPO_URL, "ceiling": config.PUBLIC_BUDGET_CEILING_EUR})


@app.post("/api/public/key-check")
async def public_key_check(request: Request):
    if not _check_window.allow(_client_ip(request), 30):
        return JSONResponse({"error": "Too many checks. Wait a bit."}, status_code=429)
    ok = await app.state.check_key(_key_of(request))
    if ok is None:
        return JSONResponse({"error": "The key could not be checked right now. Try again in a minute."}, status_code=503)
    if not ok:
        return JSONResponse({"error": "Key invalid."}, status_code=401)
    return {"ok": True}


class PublicPitchCheck(BaseModel):
    text: str = Field(default="", max_length=600)
    name: str = Field(default="", max_length=120)


@app.post("/api/public/pitch-check")
async def public_pitch_check(request: Request, body: PublicPitchCheck):
    """The same mechanical rules as in the private mode. Stores nothing, calls no model."""
    if not _check_window.allow(_client_ip(request), 60):
        return JSONResponse({"error": "Too many checks. Wait a bit."}, status_code=429)
    text = " ".join(body.text.split())
    problems = lint_pitch(text, [n for n in (body.name, body.name.split(":")[0].split(" - ")[0].strip()) if n]) if text else []
    return {"ok": not problems, "problems": problems}


class PublicIntake(BaseModel):
    url: str = Field(default="", max_length=2000)
    goals: str = Field(default="", max_length=4000)
    description: str = Field(default="", max_length=1000)


@app.post("/api/public/intake")
async def public_intake(request: Request, body: PublicIntake):
    if not _intake_window.allow(_client_ip(request), 20):
        return JSONResponse({"error": "Too many requests from this address. Try again later."}, status_code=429)
    key = _key_of(request)
    ok = await app.state.check_key(key)
    if ok is None:
        return JSONResponse({"error": "The key could not be checked right now. Try again in a minute."}, status_code=503)
    if not ok:
        return JSONResponse({"error": "Key invalid."}, status_code=401)
    started = now()
    try:
        data, usage = await intake.run_intake(body.url, body.goals, body.description, fetch_page=app.state.fetch_page, propose=app.state.propose, api_key=key)
    except intake.IntakeInvalid as exc:
        return JSONResponse({"error": exc.reason}, status_code=422)
    except intake.IntakeNeedsDescription as exc:
        return JSONResponse({"error": exc.fetch_error, "need_description": True}, status_code=422)
    try:
        led = intake.intake_ledger_line(None, "public", usage, started)
        repo().append_ledger(led)
    except Exception:  # noqa: BLE001 - accounting must not break the visitor's intake
        log.warning("public intake ledger line failed")
    data.pop("page", None)  # the browser needs the card and the goals, not the page text
    return {"url": data["url"], "card": data["card"], "pitch_line": data.get("pitch_line") or "", "goals": data["goals"], "proposal_error": data["proposal_error"], "fetch_error": data["fetch_error"], "description": data["description"], "cost_eur": (usage or {}).get("cost_eur", 0.0)}


class PublicFeedback(BaseModel):
    kind: str = Field(pattern="^(up|down|edit)$")
    comment: str = Field(default="", max_length=500)
    card_title: str = Field(default="", max_length=300)
    card_url: str = Field(default="", max_length=600)
    original: str = Field(default="", max_length=900)
    final: str = Field(default="", max_length=900)


class PublicProject(BaseModel):
    name: str = Field(max_length=120)
    url: str = Field(default="", max_length=2000)
    one_liner: str = Field(default="", max_length=500)
    problem: str = Field(default="", max_length=500)
    pitch_line: str = Field(default="", max_length=600)  # only a sentence the visitor confirmed in the browser
    audience: str = Field(default="", max_length=300)
    goals: list[str] = Field(default_factory=list, max_length=20)
    feedback: list[PublicFeedback] = Field(default_factory=list, max_length=20)
    seen_urls: list[str] = Field(default_factory=list, max_length=200)
    excluded_urls: list[str] = Field(default_factory=list, max_length=100)
    excluded_authors: list[str] = Field(default_factory=list, max_length=100)


class PublicRun(BaseModel):
    project: PublicProject
    budget: float | None = Field(default=None, ge=0, le=100)


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@app.post("/api/public/run")
async def public_run(request: Request, body: PublicRun):
    key = _key_of(request)
    ok = await app.state.check_key(key)  # free call; a bad key never reaches a paid call
    if ok is None:
        return JSONResponse({"error": "The key could not be checked right now. Try again in a minute."}, status_code=503)
    if not ok:
        return JSONResponse({"error": "Key invalid."}, status_code=401)
    if not [g for g in body.project.goals if g.strip()]:
        return JSONResponse({"error": "Add at least one goal first."}, status_code=422)
    if not _run_window.allow(_client_ip(request), config.PUBLIC_RATE_PER_IP_PER_HOUR):
        return JSONResponse({"error": "Too many runs from this address this hour. Try again later."}, status_code=429)
    pitch = " ".join(body.project.pitch_line.split())
    if pitch:
        problems = lint_pitch(pitch, [n for n in (body.project.name, body.project.name.split(":")[0].split(" - ")[0].strip()) if n])
        if problems:
            return JSONResponse({"error": "The project sentence needs a change: " + "; ".join(problems) + "."}, status_code=422)
    slot = await asyncio.to_thread(repo().acquire_slot, config.PUBLIC_MAX_CONCURRENT, 1800.0)
    if slot is None:
        return JSONResponse({"error": "Three runs are going right now. Try again in a few minutes."}, status_code=429)

    p = body.project
    pinput = ProjectInput(
        name=p.name, url=p.url, one_liner=p.one_liner, problem=p.problem.strip(), pitch_line=pitch, audience=p.audience, goals=[g.strip()[:200] for g in p.goals if g.strip()],
        feedback=[f.model_dump() for f in p.feedback], seen_urls=set(p.seen_urls), excluded_urls=set(p.excluded_urls),
        excluded_authors={a.lower() for a in p.excluded_authors},
    )
    queue: asyncio.Queue = asyncio.Queue()
    task = asyncio.create_task(app.state.run_public(pinput, key, queue.put_nowait, budget_override=body.budget, ledger_repo=repo(), http=app.state.public_http))

    async def events():
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    if task.done() and queue.empty():
                        break
                    yield ": keep-alive\n\n"
                    continue
                yield _sse(event)
                if event.get("type") == "done":
                    break
            if task.done() and task.exception():
                log.warning("public run ended with %s", type(task.exception()).__name__)
                yield _sse({"type": "error", "message": "The run ended unexpectedly."})
        finally:
            if not task.done():  # the tab closed: stop paying
                task.cancel()
            await asyncio.to_thread(repo().release_slot, slot)

    return StreamingResponse(events(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})

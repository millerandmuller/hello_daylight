"""Test doubles: a scripted web, a scripted model gateway. No network, no key, no money."""

import json
from datetime import date, datetime, timedelta, timezone

import httpx

from app import config
from app.engine import ProjectInput
from app.llm import AgentResult, CallFailed
from app.meter import RunMeter
from app.schemas import Critique, Curation, Draft, Evaluation, Finding, Pick, Plan, ScoutOutput, TaskSpec


def days_ago(n: int) -> str:
    return (datetime.now(timezone.utc).date() - timedelta(days=n)).isoformat()


class SimulatedCrash(BaseException):
    """The process dies. A BaseException so no engine code converts it into a tidy failure."""


# --- the scripted web ----------------------------------------------------------------------------
def hn_hit(oid: str, title: str, text: str, author: str = "asker", age: int = 5) -> dict:
    created = f"{days_ago(age)}T10:00:00.000Z"
    return {"objectID": oid, "title": title, "story_text": text, "author": author, "created_at": created}


def page_html(title: str, body: str, *, published: str | None = None, author: str | None = None, author_url: str | None = None, links: tuple[str, ...] = ()) -> str:
    meta = ""
    if published:
        meta += f'<meta property="article:published_time" content="{published}T08:00:00Z">'
    if author:
        meta += f'<meta name="author" content="{author}">'
    rel = f'<a rel="author" href="{author_url}">{author}</a>' if author_url and author else ""
    anchors = "".join(f'<a href="{l}">link</a>' for l in links)
    return f"<html><head><title>{title}</title>{meta}</head><body><h1>{title}</h1><p>{body}</p>{rel}{anchors}</body></html>"


class MockWeb:
    """Routes by host+path. `status` overrides make a link answer 404 and so on."""

    def __init__(self) -> None:
        self.hn_hits: list[dict] = []
        self.pages: dict[str, tuple[int, str, str]] = {}  # url -> (status, content_type, body)
        self.feeds: dict[str, str] = {}
        self.github_items: list[dict] = []
        self.requests: list[tuple[str, str]] = []

    def add_page(self, url: str, html: str, status: int = 200) -> None:
        self.pages[url] = (status, "text/html; charset=utf-8", html)

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append((request.method, url))
        host = request.url.host
        if host == "hn.algolia.com":
            return httpx.Response(200, json={"hits": self.hn_hits})
        if host == "api.github.com":
            return httpx.Response(200, json={"items": self.github_items})
        if host == "public.api.bsky.app":
            return httpx.Response(403, text="forbidden")
        if url.split("#")[0] in self.feeds:
            return httpx.Response(200, text=self.feeds[url.split("#")[0]], headers={"content-type": "application/rss+xml"})
        key = url.split("#")[0]
        if key in self.pages:
            status, ctype, body = self.pages[key]
            return httpx.Response(status, text=body, headers={"content-type": ctype})
        if host == "news.ycombinator.com":
            return httpx.Response(200, text="<html><body>item</body></html>", headers={"content-type": "text/html"})
        return httpx.Response(404, text="not found")

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


# --- the scripted model --------------------------------------------------------------------------
def default_tasks(n: int = 8) -> list[TaskSpec]:
    out = []
    for i in range(n):
        kind = "resonance" if i % 2 else "question"
        out.append(TaskSpec(role=f"Scout {i + 1}", kind=kind, instruction=f"Look for {kind} number {i + 1} on Hacker News.", tools=["hn_search", "read_page"], rationale=f"Covers angle {i + 1}."))
    return out


class FakeGateway:
    """Same surface as ModelGateway.run_agent, scripted. It still goes through the real RunMeter."""

    def __init__(self, meter: RunMeter, *, n_tasks: int = 8, tokens=(3000, 400)):
        self.meter = meter
        self.n_tasks = n_tasks
        self.tokens = tokens
        self.calls: list[tuple[str, str]] = []  # (step, name)
        self.scout_script: dict[str, callable] = {}
        self.fail_scouts: set[str] = set()
        self.crash_on_scout: str | None = None
        self.crash_after_calls: int | None = None
        self.eval_script: dict[str, Evaluation] = {}
        self.plan_note = "Moved the crew toward questions after your comment."
        self.draft_text = "You asked a real question and the answer depends on who you want to reach.\n\nI made the tool and wanted to say so plainly. Nothing to reply to here."
        self.critic_ok = True
        self.last_plan_user = ""
        self.curate_order: list[str] | None = None
        self.step_cost_calls = {"plan": 1}

    def _book(self, step: str, tier: str, chars: int = 4000) -> None:
        model = config.TIERS[tier]
        reserved = self.meter.before_call(step, tier, model, chars)
        self.meter.record(step, model, reserved, self.tokens[0], self.tokens[1])

    async def run_agent(self, *, step, tier, name, instruction, user_text, tools=None, output_schema=None, deadline_s=None, thinking="LOW", max_model_calls=12):
        self.calls.append((step, name))
        if self.crash_after_calls is not None and len(self.calls) > self.crash_after_calls:
            raise SimulatedCrash()
        self._book(step, tier, len(instruction) + len(user_text))
        if step == "plan":
            self.last_plan_user = user_text
            return AgentResult(parsed=Plan(feedback_note=self.plan_note, tasks=default_tasks(self.n_tasks)))
        if step == "scout":
            if name in self.fail_scouts:
                raise CallFailed("deadline", "scripted")
            if self.crash_on_scout == name:
                raise SimulatedCrash()
            by_name = {fn.__name__: fn for fn in (tools or [])}
            script = self.scout_script.get(name) or self._default_scout
            return AgentResult(parsed=await script(by_name, name))
        if step == "evaluate":
            tid = name.removeprefix("judge_")
            return AgentResult(parsed=self.eval_script.get(tid) or Evaluation(score=8, verdict="keep", reason="Good finds.", new_instruction=None))
        if step == "curate":
            ids = self.curate_order or [l.split(" ", 1)[0] for l in user_text.splitlines() if l[:1] == "c" and l.split(" ", 1)[0][1:].isdigit()]
            return AgentResult(parsed=Curation(picks=[Pick(item_id=i, fit="fits") for i in ids[:5]]))
        if step == "write":
            return AgentResult(parsed=Draft(text=self.draft_text))
        if step == "critic":
            return AgentResult(parsed=Critique(ok=self.critic_ok, problems=[] if self.critic_ok else ["tone"]))
        if step == "search":
            return AgentResult(text="", grounding=[])
        raise AssertionError(f"unexpected step {step}")

    async def _default_scout(self, tools, name):
        res = await tools["hn_search"]("anything")
        idx = int(name.rsplit("_t", 1)[-1]) - 1 if "_t" in name else 0
        results = res.get("results", [])
        found = []
        for r in results[idx % max(1, len(results)):][:1]:
            quote = r["snippet"][:60] if r["snippet"] and not r["snippet"].startswith("[text withheld") else ""
            found.append(Finding(url=r["url"], why="A real question.", quote=quote or "x" * 5, author_name=r["author"]))
        return ScoutOutput(findings=found)


def project_input(**kw) -> ProjectInput:
    base = dict(name="Notecast", url="https://notecast.example", one_liner="Turns daily notes into a weekly issue.", audience="newsletter writers", goals=["First users", "Feedback from writers"])
    base.update(kw)
    return ProjectInput(**base)


def hn_world(web: MockWeb, n: int = 12) -> None:
    """Enough fresh, distinct HN items for a whole crew. Each item has a text the quote can be cut from."""
    web.hn_hits = [hn_hit(str(1000 + i), f"Ask HN: how do I find users, part {i}?", f"I built a small tool number {i} and nobody has seen it yet. How do you find the first ten users for something like this?", author=f"person{i}", age=3 + i) for i in range(n)]

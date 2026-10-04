"""The night run: plan -> scouts -> verify -> evaluate/replace -> curate -> write.

The order of the steps is fixed code. Agents only fill the judgement steps. The run contract is enforced here and
in `meter.py`: step and retry caps, per-run budget with a kill switch, no paid idling, one run at a time per project,
resume from checkpoints without paying again for finished tasks.

State lives in one JSON-able dict `S` (the checkpoint). A step that is marked done in `S` is never run again.
"""

import asyncio
import copy
import json
import logging
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from . import cards as cardtext
from . import config, prompts
from .evidence import Evidence, canonical_url, domain_of, untrusted_block
from .llm import CallFailed, ModelGateway
from .meter import REASON_TEXT, RunMeter, RunStopped
from .schemas import Critique, Curation, Draft, Evaluation, Plan, ScoutOutput
from .tools import TOOL_NAMES, ToolContext, make_tools
from .verify import AGGREGATORS, VerifyContext, verify_findings
from .web import SafeHttp

log = logging.getLogger("daylight.engine")

HEARTBEAT_S = 8.0
SPEND_CHECK_S = 1.0  # money spent since the last checkpoint is saved within a second, so a hard kill loses (almost) none of it
MAX_RESUMES = 3  # a run that failed for an infrastructure reason may be resumed this often, then it stays failed
WEAK_SCORE = 4
# first labels of hosts many projects share: they say nothing about the project's own name
GENERIC_HOSTS = {"github", "gitlab", "vercel", "netlify", "pages", "app", "web", "www", "run", "herokuapp", "notion", "substack", "medium", "dev", "io", "com", "org", "net", "site"}
RELEVANCE_FLOOR = 6  # a curated pick below this does not become a card: fewer cards with a reason beat an embarrassing one


class PlanFailed(Exception):
    pass


@dataclass
class ProjectInput:
    """Everything the run knows about the project. Built by the caller; the public mode builds it from the browser."""

    name: str
    url: str = ""
    one_liner: str = ""
    audience: str = ""
    goals: list[str] = field(default_factory=list)
    feedback: list[dict] = field(default_factory=list)  # {kind: up|down|edit, comment, card_title, card_url, original, final}
    seen_urls: set[str] = field(default_factory=set)
    excluded_urls: set[str] = field(default_factory=set)
    excluded_authors: set[str] = field(default_factory=set)
    recent_authors: dict[str, str] = field(default_factory=dict)
    used_sources: list[str] = field(default_factory=list)  # domains of recent cards, for the planner
    project_id: str | None = None

    def names(self) -> list[str]:
        host = domain_of(self.url).split(".")[0] if self.url else ""
        if host in GENERIC_HOSTS:  # a project that lives on a shared host is not called after the host
            host = ""
        return [n for n in {self.name, self.name.split(":")[0].split(" - ")[0].strip(), host} if n]


class RunIO(ABC):
    """Where a run keeps its state. Private runs: the repo. Public runs: nowhere."""

    persistent = False

    @abstractmethod
    async def load(self) -> dict | None: ...

    @abstractmethod
    async def save(self, state: dict) -> None: ...

    @abstractmethod
    async def view(self, patch: dict) -> None:
        """Fields the surface shows: plan_view, feedback_note, cards, status, notes."""

    @abstractmethod
    async def beat(self) -> tuple[bool, bool]:
        """(lock still ours, cancel requested)."""

    @abstractmethod
    async def finish(self, run_patch: dict, ledger_line: dict, project_patch: "callable | None") -> None: ...

    def emit(self, event: dict) -> None:
        """Live events for a streaming surface. Default: nothing."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _raise_first(results) -> None:
    """gather(return_exceptions=True) hands back every exception, even BaseExceptions. A stop or a crash must still stop us."""
    for r in results:
        if isinstance(r, RunStopped):
            raise r
    for r in results:
        if isinstance(r, BaseException) and not isinstance(r, CallFailed):
            raise r


def _brief_items(items: list[dict], limit: int = 4) -> str:
    lines = []
    for it in items[:limit]:
        lines.append(f"- [{it['kind']}] {it['title'][:100]} ({it['source']}, {it['date']}): {it['why'][:160]}")
    return "\n".join(lines) or "(nothing verified)"


def describe_crash(exc: BaseException) -> tuple[str, bool]:
    """-> (reason the owner reads, resumable). Trouble with storage or the network is worth resuming from the
    checkpoint; a fault in our own code would fail again at the same place, so it is not."""
    chain, seen = [], set()
    e: BaseException | None = exc
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        chain.append(e)
        e = e.__cause__ or e.__context__
    for e in chain:
        mod = type(e).__module__ or ""
        text = str(e)
        if mod.startswith(("google.api_core", "google.cloud", "grpc")) or "Failed to commit transaction" in text:
            what = "too many writes at once" if ("contention" in text or "commit transaction" in text or type(e).__name__ == "Aborted") else type(e).__name__
            return f"storage error: the database did not accept a write ({what})", True
        if isinstance(e, (ConnectionError, TimeoutError)):
            return f"network error: {type(e).__name__}", True
    return f"internal error: {type(exc).__name__}", False


class NightRun:
    def __init__(
        self,
        *,
        project: ProjectInput,
        contract: config.RunContract,
        api_key: str,
        io: RunIO,
        trigger: str,
        run_id: str | None = None,
        http: SafeHttp | None = None,
        gateway: ModelGateway | None = None,
    ):
        self.p = project
        self.contract = contract
        self.io = io
        self.trigger = trigger
        self.run_id = run_id or uuid.uuid4().hex[:16]
        self.meter = RunMeter(contract)
        self.gateway = gateway or ModelGateway(api_key, self.meter)
        if gateway is not None:
            gateway.meter = self.meter  # a scripted gateway counts against this run's contract
        self.http = http or SafeHttp()
        self._own_http = http is None
        self.S: dict = {}
        self.started_at = _now()
        self.resumed_at: list[str] = []
        self.notes: set[str] = set()
        self._save_lock = asyncio.Lock()
        self._view_lock = asyncio.Lock()
        self._booked_eur = 0.0  # cost an earlier, failed attempt of this run already wrote to the ledger
        self.today = datetime.now(timezone.utc).date()
        self.vc: VerifyContext | None = None

    # ------------------------------------------------------------------------------------------
    # state plumbing
    # ------------------------------------------------------------------------------------------
    async def _checkpoint(self) -> None:
        async with self._save_lock:
            self.S["meter"] = self.meter.state()
            self.S["notes"] = sorted(self.notes)
            await self.io.save(copy.deepcopy(self.S))

    def _plan_view(self) -> list[dict]:
        out = []
        for tid, t in self.S.get("tasks", {}).items():
            out.append(
                {
                    "id": tid,
                    "role": t["spec"]["role"],
                    "kind": t["spec"]["kind"],
                    "instruction": t["spec"]["instruction"],
                    "rationale": t["spec"]["rationale"],
                    "tools": t["spec"]["tools"],
                    "status": t["status"],
                    "found": len(t["items"]),
                    "reason": t.get("reason", ""),
                    "history": t.get("history", []),
                    "dropped": t.get("dropped", [])[:6],
                }
            )
        return out

    async def _flush_view(self, **extra) -> None:
        """The view is a picture of the checkpoint for the morning desk. Flushes go one at a time (six scouts writing the
        same document at once is what made the database give up), and a write that still fails costs the picture one
        refresh, never the run: the next flush carries the whole state again."""
        async with self._view_lock:
            patch = {"plan_view": self._plan_view(), "feedback_note": self.S.get("feedback_note", ""), "notes": sorted(self.notes), "cards": self.S.get("cards", []), **extra}
            try:
                await self.io.view(patch)
            except Exception as exc:  # noqa: BLE001 - degrade: the view is display, the checkpoint is the truth
                log.warning("view update skipped: %s", type(exc).__name__)

    def _event(self, kind: str, **data) -> None:
        self.io.emit({"type": kind, **data})

    async def _heartbeat(self) -> None:
        try:
            while True:
                waited, saved_at = 0.0, self.meter.spent_eur
                while waited < HEARTBEAT_S:
                    step = min(SPEND_CHECK_S, HEARTBEAT_S - waited)
                    await asyncio.sleep(step)
                    waited += step
                    if self.meter.spent_eur != saved_at:
                        saved_at = self.meter.spent_eur
                        await self._checkpoint()
                ours, cancel = await self.io.beat()
                if not ours:
                    self.meter.stop("cancelled", "another process took over this run")
                    return
                if cancel:
                    self.meter.cancelled = True
                    self.meter.stop("cancelled")
                await self._checkpoint()
        except asyncio.CancelledError:
            raise

    # ------------------------------------------------------------------------------------------
    # context texts
    # ------------------------------------------------------------------------------------------
    def _project_text(self) -> str:
        p = self.p
        goals = "\n".join(f"- {g}" for g in p.goals) or "- (none given)"
        return (
            f"Project: {p.name}\nAddress: {p.url or '(none)'}\nWhat it does: {p.one_liner or '(unknown)'}\n"
            f"Probably for: {p.audience or '(unknown)'}\nGoals the owner confirmed:\n{goals}"
        )

    def _feedback_text(self) -> str:
        if not self.p.feedback:
            return "(no feedback since the last night)"
        lines = []
        for fb in self.p.feedback[:20]:
            if fb.get("kind") == "edit":
                lines.append(f"- edited a draft for '{fb.get('card_title', '')}'. Before: {fb.get('original', '')[:300]} After: {fb.get('final', '')[:300]}")
            else:
                verdict = "thumbs up" if fb.get("kind") == "up" else "thumbs down"
                lines.append(f"- {verdict} on '{fb.get('card_title', '')}' ({fb.get('card_url', '')}): {fb.get('comment') or '(no comment)'}")
        return untrusted_block("OWNER FEEDBACK", "\n".join(lines))

    # ------------------------------------------------------------------------------------------
    # the run
    # ------------------------------------------------------------------------------------------
    async def execute(self) -> dict:
        cp = await self.io.load()
        if cp:
            self.S = cp
            self.meter = RunMeter(self.contract, cp.get("meter"))
            self.gateway.meter = self.meter
            self.notes = set(cp.get("notes", []))
            self.started_at = cp.get("started_at", self.started_at)
            self.resumed_at = list(cp.get("resumed_at", [])) + [_now()]
            self.S["resumed_at"] = self.resumed_at
            self._booked_eur = float(cp.get("booked_eur", 0.0) or 0.0)
            lost = self.meter.book_lost_in_flight(cp.get("meter") or {})
            if lost:
                self.notes.add(f"{lost:.4f} EUR of calls under way at the abort counted as spent (estimate)")
            again = ("running", "replacing", "stopped", "not_started") if self.S.pop("resumable", False) else ("running", "replacing")
            for t in self.S.get("tasks", {}).values():  # a task that was running when the process died (or failed) is pending again
                if t["status"] in again:
                    t["status"], t["reason"] = "pending", ""
        else:
            self.S = {"run_id": self.run_id, "started_at": self.started_at, "stage": "plan", "tasks": {}, "cards": [], "picks": None}
        self.vc = self._verify_context()
        beat = asyncio.create_task(self._heartbeat())
        status, reason = "ok", ""
        try:
            await self._plan()
            await self._scouts()
            await self._evaluate_and_replace()
            await self._curate()
            await self._write_cards()
            status, reason = self._final_status()
        except RunStopped as stop:
            status = "cancelled" if stop.reason == "cancelled" else "stopped"
            reason = stop.reason
            self.S["stop_detail"] = stop.detail
        except asyncio.CancelledError:  # the process is told to stop (a closed tab, a shutdown): account for it, then let it go
            beat.cancel()
            self.meter.stop("cancelled", "client gone")
            await asyncio.shield(self._finalize("cancelled", "cancelled"))
            raise
        except PlanFailed as exc:
            status, reason = "failed", str(exc)
        except CallFailed as exc:  # the plan or curation could not be made: the provider is down or answers garbage
            status, reason = "failed", f"model: {exc.reason}"
        except Exception as exc:  # noqa: BLE001 - a run never ends in a bare traceback; it ends with a reason
            log.exception("run crashed")
            reason, resumable = describe_crash(exc)
            status = "failed"
            self.S["resumable"] = resumable and len(self.resumed_at) < MAX_RESUMES and self.io.persistent
        finally:
            beat.cancel()
            try:
                await beat
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        ledger = await self._finalize(status, reason)
        if self._own_http:
            await self.http.close()
        return ledger

    def _verify_context(self) -> VerifyContext:
        p = self.p
        vc = VerifyContext(
            http=self.http,
            seen_urls={canonical_url(u) for u in p.seen_urls},
            excluded_urls={canonical_url(u) for u in p.excluded_urls},
            excluded_authors=set(p.excluded_authors),
            recent_authors=dict(p.recent_authors),
        )
        # a resumed run must not count its own earlier findings as duplicates of themselves
        for t in self.S.get("tasks", {}).values():
            for it in t.get("items", []):
                vc.run_urls.add(canonical_url(it["url"]))
                if it["kind"] == "resonance" and it.get("author"):
                    from .verify import author_key

                    vc.run_authors.add(author_key(it["author"]))
        return vc

    def _final_status(self) -> tuple[str, str]:
        missing = [t for t in self.S["tasks"].values() if t["status"] == "missing"]
        if not self.S["cards"]:
            if missing:
                return "partial", f"{len(missing)} of {len(self.S['tasks'])} scouts did not finish, no openings"
            return "ok", "no_openings"
        if missing:
            return "partial", f"{len(missing)} scout(s) did not finish"
        return "ok", ""

    # ------------------------------------------------------------------------------------------
    # N2: the lead plans the crew
    # ------------------------------------------------------------------------------------------
    async def _plan(self) -> None:
        if self.S["tasks"]:
            return
        c = self.contract
        instruction = prompts.PLAN_INSTRUCTION.format(
            today=self.today.isoformat(), n_min=c.min_tasks, n_max=c.max_tasks, n_target=(c.min_tasks + c.max_tasks) // 2 + 1, tools=prompts.tools_text(), voice=prompts.VOICE, untrusted=prompts.UNTRUSTED_RULE
        )
        used = ", ".join(self.p.used_sources[:20]) or "(nothing yet)"
        user = f"{self._project_text()}\n\nAlready used (domains of recent openings): {used}\n\nOwner feedback since the last night:\n{self._feedback_text()}"
        res = await self.gateway.run_agent(step="plan", tier="strong", name="lead_planner", instruction=instruction, user_text=user, output_schema=Plan, deadline_s=240)
        plan: Plan = res.parsed
        problems = self._plan_problems(plan)
        if problems:
            repair = f"{user}\n\nYour previous plan had these problems, fix them and answer again: {'; '.join(problems)}"
            self.meter.count_retry("plan repair")
            res = await self.gateway.run_agent(step="plan", tier="strong", name="lead_planner", instruction=instruction, user_text=repair, output_schema=Plan, deadline_s=240)
            plan = res.parsed
        tasks = [t for t in plan.tasks if t.instruction.strip()][: c.max_tasks]
        if len(tasks) < 3:
            raise PlanFailed("the lead produced no usable crew")
        if len(tasks) < c.min_tasks:
            self.notes.add(f"crew smaller than planned: {len(tasks)} instead of at least {c.min_tasks}")
        for i, spec in enumerate(tasks, 1):
            spec.tools = [t for t in dict.fromkeys(spec.tools) if t in TOOL_NAMES] or ["web_search", "read_page"]
            if "read_page" not in spec.tools and spec.kind == "resonance":
                spec.tools.append("read_page")
            self.S["tasks"][f"t{i}"] = {"spec": spec.model_dump(), "status": "pending", "items": [], "dropped": [], "history": [], "replacements": 0, "attempts": 0}
        note = plan.feedback_note.strip()
        if self.p.feedback and not note:
            note = f"Read your feedback ({len(self.p.feedback)} notes). The plan did not need a change."
        self.S["feedback_note"] = note if self.p.feedback else ""
        self.S["stage"] = "scouts"
        await self._checkpoint()
        await self._flush_view(status="running")
        self._event("plan", tasks=self._plan_view(), feedback_note=self.S["feedback_note"])

    def _plan_problems(self, plan: Plan) -> list[str]:
        c, problems = self.contract, []
        n = len([t for t in plan.tasks if t.instruction.strip()])
        if n < c.min_tasks:
            problems.append(f"only {n} scouts, at least {c.min_tasks} are required")
        if n > c.max_tasks:
            problems.append(f"{n} scouts, at most {c.max_tasks} are allowed")
        if not any(t.kind == "resonance" for t in plan.tasks):
            problems.append("no 'resonance' scout")
        if not any(t.kind == "question" for t in plan.tasks):
            problems.append("no 'question' scout")
        if self.p.feedback and not plan.feedback_note.strip():
            problems.append("feedback exists but feedback_note is empty")
        return problems

    # ------------------------------------------------------------------------------------------
    # N3 + N4: scouts search, code verifies
    # ------------------------------------------------------------------------------------------
    async def _scouts(self) -> None:
        pending = [tid for tid, t in self.S["tasks"].items() if t["status"] == "pending"]
        sem = asyncio.Semaphore(self.contract.scout_concurrency)

        async def one(tid: str):
            async with sem:
                await self._run_scout(tid)

        results = await asyncio.gather(*(one(t) for t in pending), return_exceptions=True)
        _raise_first(results)
        self.S["stage"] = "evaluate"
        await self._checkpoint()

    async def _run_scout(self, tid: str, instruction: str | None = None) -> None:
        t = self.S["tasks"][tid]
        spec = t["spec"]
        self.meter.check_alive()
        t["status"] = "running"
        t["attempts"] += 1
        await self._flush_view(status="running")
        self._event("task", id=tid, status="running")
        if instruction is None and t["history"]:
            instruction = t["history"][-1].get("new_instruction")  # the newest instruction wins, also after a resume
        ev = Evidence()
        ctx = ToolContext(ev, self.http, self.gateway, self.notes)
        tools = [fn for fn in make_tools(ctx) if fn.__name__ in spec["tools"]]
        body = prompts.SCOUT_INSTRUCTION.format(role=spec["role"], instruction=instruction or spec["instruction"], untrusted=prompts.UNTRUSTED_RULE)
        user = f"Today is {self.today.isoformat()}.\n{self._project_text()}\nTask kind: {spec['kind']}"
        try:
            res = await self.gateway.run_agent(
                step="scout", tier="cheap", name=f"scout_{tid}", instruction=body, user_text=user, tools=tools,
                output_schema=ScoutOutput, deadline_s=self.contract.scout_deadline_s, max_model_calls=10,
            )
        except CallFailed as exc:  # R3: degrade, name the missing part, keep going
            t["status"] = "missing"
            t["reason"] = f"did not finish ({exc.reason})"
            self.notes.add(f"{spec['role']}: {t['reason']}")
            await self._checkpoint()
            await self._flush_view(status="running")
            self._event("task", id=tid, status="missing", reason=t["reason"])
            return
        items, dropped = await verify_findings(res.parsed.findings, spec["kind"], ev, self.vc)
        for sus in ctx.suspicious:
            dropped.append({"url": sus["url"], "reason": "suspicious: text that looks like an instruction to a model"})
        t["items"] += [dict(it, task_id=tid, weak=False) for it in items]
        t["dropped"] += dropped
        t["status"] = "done"
        t["reason"] = "" if items else (res.parsed.nothing_found_because or "nothing verified")
        await self._checkpoint()
        await self._flush_view(status="running")
        self._event("task", id=tid, status="done", found=len(t["items"]))

    # ------------------------------------------------------------------------------------------
    # N5 + N5b: the lead judges, weak scouts are replaced (capped)
    # ------------------------------------------------------------------------------------------
    async def _evaluate_one(self, tid: str) -> Evaluation:
        t = self.S["tasks"][tid]
        spec = t["spec"]
        instruction = prompts.EVAL_INSTRUCTION.format(today=self.today.isoformat(), voice=prompts.VOICE, untrusted=prompts.UNTRUSTED_RULE)
        dropped = "; ".join(f"{d['reason']}" for d in t["dropped"][:6]) or "none"
        user = (
            f"{self._project_text()}\n\nScout: {spec['role']} (kind {spec['kind']})\nIts instruction: {spec['instruction'][:700]}\n"
            f"Verified results:\n{_brief_items(t['items'])}\nDropped by the checks (reasons): {dropped}\nScout's own note: {t.get('reason') or '-'}"
        )
        res = await self.gateway.run_agent(step="evaluate", tier="mid", name=f"judge_{tid}", instruction=instruction, user_text=user, output_schema=Evaluation, deadline_s=90, max_model_calls=2)
        ev: Evaluation = res.parsed
        ev.score = max(0, min(10, ev.score))
        if not [i for i in t["items"] if not i.get("weak")]:
            ev.verdict = "replace"  # nothing usable is never a keep
        if ev.verdict == "replace" and not (ev.new_instruction or "").strip():
            ev.new_instruction = f"{spec['instruction']}\nThe earlier attempt found nothing usable. Use different search words and other sources."
        return ev

    async def _evaluate_and_replace(self) -> None:
        c = self.contract
        for round_no in (1, 2):
            tasks = self.S["tasks"]
            if round_no == 1:
                candidates = [tid for tid, t in tasks.items() if t["status"] == "done" and "evaluation" not in t]
            else:  # a replacement that found nothing gets one more judgement and, if allowed, one more replacement
                candidates = [tid for tid, t in tasks.items() if t["status"] == "done" and t.get("needs_second_look") and t["replacements"] < c.max_replacements_task]
            if not candidates or self.meter.replacements >= c.max_replacements_run:
                break
            sem = asyncio.Semaphore(4)

            async def judge(tid):
                async with sem:
                    try:
                        return tid, await self._evaluate_one(tid)
                    except CallFailed:
                        return tid, None  # an unjudged scout is kept as it is

            judged = await asyncio.gather(*(judge(t) for t in candidates), return_exceptions=True)
            _raise_first(judged)
            for tid, ev in judged:
                t = tasks[tid]
                t["needs_second_look"] = False
                if ev is not None:
                    t["evaluation"] = {"score": ev.score, "verdict": ev.verdict, "reason": ev.reason}
                    t["reason"] = ev.reason
                else:
                    t["evaluation"] = {"score": 5, "verdict": "keep", "reason": "not judged"}
            await self._checkpoint()
            await self._flush_view(status="running")
            weak = sorted([(ev.score, tid, ev) for tid, ev in judged if ev is not None and ev.verdict == "replace"], key=lambda x: x[0])
            room = max(0, c.max_replacements_run - self.meter.replacements)
            todo = [w for w in weak if tasks[w[1]]["replacements"] < c.max_replacements_task][:room]
            if not todo:
                break
            await self._replace(todo)
            self.S["stage"] = "replace"
            await self._checkpoint()

    async def _replace(self, todo: list[tuple[int, str, Evaluation]]) -> None:
        sem = asyncio.Semaphore(self.contract.scout_concurrency)

        async def one(score, tid, ev):
            async with sem:
                t = self.S["tasks"][tid]
                self.meter.count_retry(f"replace {tid}", replacement=True)  # counts against max_retries and the replacement caps
                for it in t["items"]:
                    it["weak"] = True
                t["replacements"] += 1
                t["history"].append({"score": score, "reason": ev.reason, "new_instruction": ev.new_instruction})
                t["status"] = "replacing"
                before = len(t["items"])
                await self._flush_view(status="running")
                self._event("replace", id=tid, reason=ev.reason, new_instruction=ev.new_instruction)
                await self._run_scout(tid, instruction=ev.new_instruction)
                new_items = len(t["items"]) - before
                t["history"][-1]["found"] = new_items
                t["needs_second_look"] = new_items == 0
                if new_items:
                    t["evaluation"] = {"score": 7, "verdict": "keep", "reason": f"The replacement found {new_items}."}
                    t["reason"] = t["evaluation"]["reason"]
                await self._flush_view(status="running")

        results = await asyncio.gather(*(one(*w) for w in todo), return_exceptions=True)
        _raise_first(results)

    # ------------------------------------------------------------------------------------------
    # N6: the curator picks the openings
    # ------------------------------------------------------------------------------------------
    def _all_items(self) -> list[dict]:
        items = []
        for tid, t in self.S["tasks"].items():
            score = (t.get("evaluation") or {}).get("score", 5)
            for it in t["items"]:
                items.append(dict(it, _score=score - (3 if it.get("weak") else 0)))
        items.sort(key=lambda i: i["date"] or "", reverse=True)  # at an equal score the younger thread comes first (stable sort)
        items.sort(key=lambda i: -i["_score"])
        for n, it in enumerate(items, 1):
            it["item_id"] = f"c{n}"
        return items

    async def _curate(self) -> None:
        if self.S.get("picks") is not None:
            return
        c = self.contract
        items = self._all_items()[:40]
        if not items:
            self.S["picks"] = []
            self.notes.add("no verified findings tonight")
            await self._checkpoint()
            return
        by_id = {i["item_id"]: i for i in items}
        picks: list[str] = []
        curated = False
        # the curator judges relevance even when there are only a few findings: passing the checks is not a fit
        instruction = prompts.CURATE_INSTRUCTION.format(n=c.max_cards, voice=prompts.VOICE, untrusted=prompts.UNTRUSTED_RULE)
        listing = "\n".join(f"{i['item_id']} [{i['kind']}] {' '.join(i['title'].split())[:90]} | {i['source']} | {i['date']} | {self._age_and_replies(i)} | author: {i['author'] or '-'} | {i['why'][:140]}" for i in items)
        user = f"{self._project_text()}\n\nFeedback:\n{self._feedback_text()}\n\nVerified findings:\n{untrusted_block('FINDINGS', listing)}"
        try:
            res = await self.gateway.run_agent(step="curate", tier="mid", name="curator", instruction=instruction, user_text=user, output_schema=Curation, deadline_s=120, max_model_calls=2)
            valid = [p for p in res.parsed.picks if p.item_id in by_id]
            # equal relevance: the younger thread first. The sort is stable, so the curator's own order decides everything else.
            valid.sort(key=lambda p: (-p.relevance, self._age_of(by_id[p.item_id])))
            low = [p for p in valid if p.relevance < RELEVANCE_FLOOR]
            picks = [p.item_id for p in valid if p.relevance >= RELEVANCE_FLOOR]
            curated = True
            self.S["off_topic"] = len(low)
            if low:
                self.notes.add(f"{len(low)} finding(s) left out as not close enough to the goals")
        except CallFailed:
            self.notes.add("curation fell back to the best-scored findings")
        picks = list(dict.fromkeys(picks))
        picks = self._enforce_picks(picks, items, c.max_cards, fill=not curated)
        self.S["picks"] = [by_id[i] for i in picks]
        await self._checkpoint()

    def _age_of(self, item: dict) -> int:
        age = cardtext.age_days(item.get("date"), self.today)
        return age if age is not None else 10_000

    def _age_and_replies(self, item: dict) -> str:
        age = cardtext.age_days(item.get("date"), self.today)
        parts = [f"age: {age} days" if age is not None else "age: unknown"]
        if item.get("replies") is not None:
            parts.append(f"replies: {item['replies']}")
        return " | ".join(parts)

    @staticmethod
    def _enforce_picks(picks: list[str], items: list[dict], n: int, fill: bool = True) -> list[str]:
        """Code has the last word: at most n, no author or page twice, at least one resonance if one exists among the
        candidates. fill=False (the curator answered): only its relevant picks are candidates, nothing is topped up."""
        by_id = {i["item_id"]: i for i in items}
        chosen, authors, urls = [], set(), set()

        def ok(it):
            a = (it["author"] or "").lower() if it["kind"] == "resonance" else ""
            return it["url"] not in urls and not (a and a in authors)

        pool = picks + [i["item_id"] for i in items] if fill else list(picks)
        for iid in pool:
            it = by_id[iid]
            if iid in chosen or not ok(it):
                continue
            chosen.append(iid)
            urls.add(it["url"])
            if it["kind"] == "resonance" and it["author"]:
                authors.add(it["author"].lower())
            if len(chosen) >= n:
                break
        if not any(by_id[i]["kind"] == "resonance" for i in chosen):
            extra = next((by_id[i] for i in pool if by_id[i]["kind"] == "resonance" and ok(by_id[i])), None)
            if extra:
                if len(chosen) >= n:
                    chosen.pop()
                chosen.append(extra["item_id"])
        return chosen

    # ------------------------------------------------------------------------------------------
    # N7: writer and critic, at most two rounds per card
    # ------------------------------------------------------------------------------------------
    async def _write_cards(self) -> None:
        picks = self.S.get("picks") or []
        done_urls = {c["url"] for c in self.S["cards"]}
        todo = [p for p in picks if p["url"] not in done_urls]
        self.S["stage"] = "write"
        self._drafts = {c["id"]: c["draft"] for c in self.S["cards"]}  # newest draft per card id, for the closing check
        sem = asyncio.Semaphore(3)

        async def one(item: dict):
            async with sem:
                card = await self._write_card(item)
                self.S["cards"].append(card)
                self.S["cards"].sort(key=lambda c: c["rank"])
                await self._checkpoint()
                await self._flush_view(status="running")
                self._event("card", card=card)

        results = await asyncio.gather(*(one(p) for p in todo), return_exceptions=True)
        _raise_first(results)
        self.S["stage"] = "done"

    async def _write_card(self, item: dict) -> dict:
        c = self.contract
        rank = (self.S["picks"] or []).index(item) if item in (self.S["picks"] or []) else 99
        card_id = f"k{rank + 1}"
        kind = item["kind"]
        # A reply in a stranger's issue answers the question and carries no project; the owner adds the link with one click.
        plain = kind == "question" and item["source"] == "github"
        if plain:
            writer_instr = prompts.WRITE_QUESTION_PLAIN.format(voice=prompts.VOICE, untrusted=prompts.UNTRUSTED_RULE)
        else:
            tmpl = prompts.WRITE_QUESTION if kind == "question" else prompts.WRITE_RESONANCE
            writer_instr = tmpl.format(project=self.p.name, url=self.p.url or "", pitch=prompts.PITCH_RULE, voice=prompts.VOICE, untrusted=prompts.UNTRUSTED_RULE)
        source = (
            f"Source: {item['title']} ({item['url']}), written {item['date']}"
            f"{', by ' + item['author'] if item['author'] else ''}.\nQuote: {item['quote']}\nWhy it fits: {item['why']}\nPage facts:\n"
            f"{untrusted_block('PAGE', item['text'])}"
        )
        base = f"{self._project_text()}\n\n{source}"
        draft, problems, original = "", [], ""
        names = self.p.names()
        lint: list[str] = []
        for round_no in range(1, c.max_rounds_write + 1):
            ask = base if not draft else f"{base}\n\nYour previous draft:\n{draft}\n\nFix these problems and write the draft again: {'; '.join(problems)}"
            res = await self.gateway.run_agent(step="write", tier="mid", name=f"writer_{kind}", instruction=writer_instr, user_text=ask, output_schema=Draft, deadline_s=90, max_model_calls=2)
            draft = res.parsed.text.strip()
            original = original or draft
            lint = self._lint(draft, names, plain, card_id)
            if lint and round_no < c.max_rounds_write:
                problems = lint  # a mechanical failure needs no editor
                continue
            crit_instr = prompts.CRITIC_INSTRUCTION.format(rules=prompts.DRAFT_RULES, lint="; ".join(lint) or "none", untrusted=prompts.UNTRUSTED_RULE)
            cres = await self.gateway.run_agent(step="critic", tier="mid", name="critic", instruction=crit_instr, user_text=f"{base}\n\nDraft to check:\n{draft}", output_schema=Critique, deadline_s=90, max_model_calls=2)
            if cres.parsed.ok and not lint:
                problems = []
                break
            problems = (cres.parsed.problems or []) + lint
        draft = prompts.mechanical_fix(draft)
        if plain:  # the last word is code: whatever the model did, this reply carries no project
            draft = cardtext.strip_project(draft, names, self.p.url) or draft
        left = self._lint(draft, names, plain, card_id)
        self._drafts[card_id] = draft
        route_words, route_url = cardtext.route_text({"kind": kind, "source": item["source"], "contact_route": item["contact_route"], "url": item["url"]})
        return {
            "id": card_id,
            "rank": rank,
            "kind": kind,
            "title": item["title"],
            "url": item["url"],
            "date": item["date"],
            "date_basis": item.get("date_basis", "api"),
            "age_days": cardtext.age_days(item["date"], self.today),
            "replies": item.get("replies") if item["source"] == "hn" else None,
            "source": item["source"],
            "author": item["author"],
            "quote": item["quote"],
            "why": item["why"],
            "contact_route": item["contact_route"],
            "contact_source_url": item["contact_source_url"],
            "route_words": route_words,
            "route_url": route_url,
            "link_sentence": cardtext.link_sentence(self.p.name, self.p.url) if plain and self.p.url else None,
            "link_added": False,
            "draft": draft,
            "original_draft": original,
            "needs_attention": left,
            "state": "new",
            "thumb": None,
            "comment": "",
            "task_id": item.get("task_id"),
        }

    def _lint(self, draft: str, names: list[str], plain: bool, card_id: str) -> list[str]:
        """The rule-based problems of a draft, in the same place for every round: style, project mentions and a closing
        that repeats another card's of this night."""
        problems = prompts.lint_draft(draft, names, project_allowed=not plain, project_url=self.p.url or "")
        self._drafts[card_id] = draft  # the other cards of this night compare against the newest version
        other = cardtext.same_closing_as(card_id, draft, {k: v for k, v in self._drafts.items() if k != card_id})
        if other:
            problems.append(f'{cardtext.closing_hint(other)} ("{cardtext.closing_paragraph(self._drafts[other])[:120]}"): write a different closing that fits this source')
        return problems

    # ------------------------------------------------------------------------------------------
    # N8: close the run, ledger line, release
    # ------------------------------------------------------------------------------------------
    async def _finalize(self, status: str, reason: str) -> dict:
        if status in ("stopped", "cancelled", "failed"):  # nothing may keep claiming to be at work
            for t in self.S.get("tasks", {}).values():
                if t["status"] in ("running", "replacing"):
                    t["status"], t["reason"] = "stopped", "The run stopped before this scout finished."
                elif t["status"] == "pending":
                    t["status"], t["reason"] = "not_started", "The run stopped before this scout started."
        m = self.meter.summary()
        ended = _now()
        replaced = [
            {"task_id": tid, "role": t["spec"]["role"], "reason": h.get("reason", ""), "found_after": h.get("found")}
            for tid, t in self.S.get("tasks", {}).items()
            for h in t.get("history", [])
        ]
        missing = [{"task_id": tid, "reason": t.get("reason", "")} for tid, t in self.S.get("tasks", {}).items() if t["status"] == "missing"]
        kill = self.meter.stopped if status in ("stopped", "cancelled") else None
        incomplete = status in ("stopped", "cancelled", "failed") or bool(missing)
        if status == "stopped" and reason in REASON_TEXT:
            headline = REASON_TEXT[reason]
        elif status == "cancelled":
            headline = REASON_TEXT["cancelled"]
        elif status == "failed" and self.S.get("resumable"):
            headline = f"This night stopped early: {reason}. The next start continues where it stopped, without paying again for what is done."
        elif status == "failed":
            headline = f"This night did not run: {reason}."
        elif status == "partial" and not self.S.get("cards"):
            headline = f"No openings tonight, and {len(missing)} scout(s) did not finish: this is not an empty result."
        elif reason == "no_openings":
            headline = "No openings tonight: nothing the checks could confirm."
        elif len(self.S.get("cards", [])) < self.contract.max_cards:
            headline = self._shortfall_note()
        else:
            headline = ""
        cost_line = f"Cost of this run: {m['cost_eur']:.2f} EUR of {m['budget_eur']:.2f} EUR, {m['calls']} model calls."
        # A resumed run that failed before already booked part of its cost: this line books only the rest, so the
        # monthly sums count every euro once. cost_total_eur is what the whole run cost.
        booked = self._booked_eur
        ledger = {
            "run_id": self.run_id,
            "project_id": self.p.project_id,
            "mode": self.contract.mode,
            "trigger": self.trigger,
            "status": status,
            "reason": reason,
            "kill_switch": kill,
            "started_at": self.started_at,
            "ended_at": ended,
            "resumed_at": self.resumed_at,
            "cost_eur": round(max(0.0, m["cost_eur"] - booked), 6),
            "cost_total_eur": m["cost_eur"],
            "booked_before_eur": booked,
            "budget_eur": m["budget_eur"],
            "calls": m["calls"],
            "retries": m["retries"],
            "replacements": m["replacements"],
            "search_queries": m["search_queries"],
            "steps": m["steps"],
            "replaced_agents": replaced,
            "missing_tasks": missing,
            "tasks": len(self.S.get("tasks", {})),
            "cards": len(self.S.get("cards", [])),
            "notes": sorted(self.notes),
            "models": dict(config.TIERS),
        }
        run_patch = {
            "status": status,
            "reason": reason,
            "headline": headline,
            "cost_line": cost_line,
            "ended_at": ended,
            "incomplete": incomplete,
            "cost_eur": m["cost_eur"],
            "resumed_at": self.resumed_at,
            "plan_view": self._plan_view(),
            "feedback_note": self.S.get("feedback_note", ""),
            "cards": self.S.get("cards", []),
            "notes": sorted(self.notes),
            "unfinished": self._unfinished() if incomplete else [],
            "resumable": bool(status == "failed" and self.S.get("resumable")),
            "ledger": ledger,
        }
        if status == "failed" and self.S.get("resumable"):
            # the checkpoint a resume starts from: it knows what is already booked
            self.S["booked_eur"] = m["cost_eur"]
            try:
                await self._checkpoint()
            except Exception:  # noqa: BLE001 - without a checkpoint the run simply is not resumable
                self.S["resumable"] = run_patch["resumable"] = False
        card_urls = [c["url"] for c in self.S.get("cards", [])]
        authors = {(c["author"] or "").lower(): ended[:10] for c in self.S.get("cards", []) if c["kind"] == "resonance" and c["author"]}
        fb_ids = [f["id"] for f in self.p.feedback if f.get("id")]

        def project_patch(doc: dict) -> None:
            seen = doc.setdefault("seen", {"urls": {}, "authors": {}})
            for u in card_urls:
                seen["urls"][canonical_url(u)] = ended[:10]
            from .verify import author_key

            for a, d in authors.items():
                seen["authors"][author_key(a)] = d
            cutoff = date.fromisoformat(ended[:10]).toordinal() - 60
            seen["urls"] = {u: d for u, d in seen["urls"].items() if date.fromisoformat(d).toordinal() >= cutoff}
            seen["authors"] = {a: d for a, d in seen["authors"].items() if date.fromisoformat(d).toordinal() >= cutoff}
            doc["last_run"] = {"run_id": self.run_id, "status": status, "ended_at": ended}

        await self.io.finish(run_patch, ledger, project_patch if self.p.project_id else None)
        self._event("done", ledger=ledger, headline=headline, cost_line=cost_line)
        log.info("run %s finished: %s %s cost=%.4f calls=%d", self.run_id, status, reason, m["cost_eur"], m["calls"])
        return ledger

    def _shortfall_note(self) -> str:
        """Fewer cards than hoped for is always explained, never silent."""
        n = len(self.S.get("cards", []))
        reasons: dict[str, int] = {}
        for t in self.S.get("tasks", {}).values():
            for d in t.get("dropped", []):
                key = d["reason"].split(":")[0]
                reasons[key] = reasons.get(key, 0) + 1
        top = sorted(reasons.items(), key=lambda kv: -kv[1])[:2]
        missing = len([t for t in self.S.get("tasks", {}).values() if t["status"] == "missing"])
        parts = [f"{count} dropped because {why}" for why, count in top]
        if self.S.get("off_topic"):
            parts.append(f"{self.S['off_topic']} left out as not close enough to your goals")
        if missing:
            parts.append(f"{missing} scout(s) did not finish")
        found = len(self._all_items())
        unchosen = found - n - int(self.S.get("off_topic") or 0)
        if unchosen > 0:
            parts.append(f"{unchosen} not chosen (the same page or writer twice, or fewer than five fitted)")
        extra = f" ({'; '.join(parts)})" if parts else ""
        return f"{n} opening{'s' if n != 1 else ''} tonight: the crew confirmed {found} finding{'s' if found != 1 else ''}{extra}."

    def _unfinished(self) -> list[dict]:
        """Findings that were verified but never became a card (the run stopped first): shown as incomplete."""
        shown = {c["url"] for c in self.S.get("cards", [])}
        out = []
        for it in self._all_items():
            if it["url"] not in shown:
                out.append({k: it[k] for k in ("title", "url", "date", "source", "kind", "why")})
        return out[:12]

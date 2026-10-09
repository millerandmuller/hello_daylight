"""N4: plain code, no model. A scout's claim becomes a result only if the facts the TOOLS saw support it.

Checks, in this order: the URL came from a tool; no instruction-like text; the quote is really on the page; the link
answers with HTTP 200; the date is stated by the source and is at most 90 days old; no duplicate; an author is used at
most once in 30 days; a contact route is kept only if it stands literally on a page the scout read.
"""

import asyncio
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from .evidence import Evidence, EvidenceItem, canonical_url, domain_of, injection_markers, is_excluded, norm_text
from .schemas import Finding
from .web import SafeHttp

MAX_AGE_DAYS = 90
AUTHOR_COOLDOWN_DAYS = 30
MIN_QUOTE_CHARS = 12
_GITHUB_THREAD = re.compile(r"^https?://(?:www\.)?github\.com/[^/]+/[^/]+/(?:issues|pull|discussions)/\d+", re.I)
NOT_AN_ARTICLE = "an issue is not an article"
AGGREGATORS = {"news.ycombinator.com", "github.com", "bsky.app", "reddit.com", "stackoverflow.com", "dev.to", "medium.com", "substack.com"}


def author_key(name: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


def today_utc() -> date:
    return datetime.now(timezone.utc).date()


@dataclass
class VerifyContext:
    http: SafeHttp
    seen_urls: set[str] = field(default_factory=set)  # shown in earlier nights
    excluded_urls: set[str] = field(default_factory=set)  # thumbs down
    excluded_authors: set[str] = field(default_factory=set)
    recent_authors: dict[str, str] = field(default_factory=dict)  # author key -> ISO date last shown
    run_urls: set[str] = field(default_factory=set)
    run_authors: set[str] = field(default_factory=set)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    today: date = field(default_factory=today_utc)


def _age_problem(day: str | None, today: date) -> str | None:
    if not day:
        return "the source states no date"
    try:
        d = date.fromisoformat(day)
    except ValueError:
        return "the date is not readable"
    if d > today + timedelta(days=1):
        return "the date lies in the future"
    if (today - d).days > MAX_AGE_DAYS:
        return f"older than {MAX_AGE_DAYS} days"
    return None


def _route_norm(route: str) -> str:
    r = route.strip().lower()
    r = re.sub(r"^mailto:", "", r)
    return r.rstrip("/")


def contact_is_literal(route: str | None, source: EvidenceItem | None) -> bool:
    """The route stands, word for word, in a link or in the text of a page the scout actually read."""
    if not route or source is None:
        return False
    want = _route_norm(route)
    if len(want) < 6:
        return False
    if any(want in _route_norm(link) for link in source.links):
        return True
    return want in source.text.lower()


async def verify_finding(finding: Finding, kind: str, evidence: Evidence, vc: VerifyContext) -> tuple[dict | None, str | None]:
    """(verified item, None) or (None, reason it was dropped)."""
    ev = evidence.get(finding.url)
    if ev is None:
        return None, "not a result of a tool"
    if is_excluded(ev.url):
        return None, "source excluded by policy (login wall or no public API)"
    markers = ev.suspicious or injection_markers(ev.title, ev.text, ev.author, finding.why, finding.quote, finding.author_name or "")
    if markers:
        return None, "suspicious: text that looks like an instruction to a model"
    quote = norm_text(finding.quote)
    if len(quote) < MIN_QUOTE_CHARS or quote not in norm_text(ev.title + " " + ev.text):
        return None, "the quote is not on the page"
    on_github_thread = bool(_GITHUB_THREAD.match(ev.url))
    if on_github_thread and kind == "resonance":
        # An issue, a discussion or a pull request is somebody's question in a tracker, never a writer's text. It goes on as a
        # question when it reads like one (the title or the quote asks something); otherwise it is dropped, with the reason.
        if "?" in finding.quote or "?" in ev.title:
            kind = "question"
        else:
            return None, NOT_AN_ARTICLE
    problem = _age_problem(ev.date, vc.today)
    if problem:
        return None, problem
    status, final_url = await vc.http.check_link(ev.url)
    if is_excluded(final_url):
        return None, "source excluded by policy (login wall or no public API)"
    if status != 200:
        return None, f"the link answered {status or 'nothing'}"
    key = canonical_url(final_url)
    akey = author_key(finding.author_name or ev.author)
    async with vc.lock:
        if key in vc.run_urls or key in vc.seen_urls or canonical_url(ev.url) in vc.seen_urls:
            return None, "duplicate"
        if key in vc.excluded_urls or canonical_url(ev.url) in vc.excluded_urls:
            return None, "same source as a card you marked as not useful"
        if akey and akey in vc.excluded_authors:
            return None, "same author as a card you marked as not useful"
        if kind == "resonance":
            if not akey:
                return None, "no author named"
            if akey in vc.run_authors:
                return None, "author already used in this run"
            last = vc.recent_authors.get(akey)
            if last:
                try:
                    if (vc.today - date.fromisoformat(last[:10])).days < AUTHOR_COOLDOWN_DAYS:
                        return None, f"author used in the last {AUTHOR_COOLDOWN_DAYS} days"
                except ValueError:
                    pass
        vc.run_urls.add(key)
        if kind == "resonance" and akey:
            vc.run_authors.add(akey)
    route, route_src = None, None
    if kind == "resonance" and finding.contact_route and finding.contact_source_url:
        src = evidence.get(finding.contact_source_url)
        if contact_is_literal(finding.contact_route, src):
            route, route_src = finding.contact_route.strip(), src.url
    return (
        {
            "url": final_url,
            "title": ev.title,
            "date": ev.date,
            "date_basis": ev.date_basis,
            "source": "github" if on_github_thread else ev.source,
            "replies": ev.replies,
            "kind": kind,
            "why": finding.why.strip(),
            "quote": finding.quote.strip(),
            "author": (finding.author_name or ev.author or "").strip(),
            "contact_route": route,
            "contact_source_url": route_src,
            "domain": domain_of(final_url),
            "text": ev.text[:1200],
        },
        None,
    )


async def verify_findings(findings: list[Finding], kind: str, evidence: Evidence, vc: VerifyContext) -> tuple[list[dict], list[dict]]:
    results = await asyncio.gather(*(verify_finding(f, kind, evidence, vc) for f in findings[:4]))
    items = [r[0] for r in results if r[0]]
    dropped = [{"url": f.url, "reason": r[1]} for f, r in zip(findings[:4], results) if r[1]]
    return items, dropped

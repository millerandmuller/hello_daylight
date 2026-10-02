"""Reads a public project page. Refuses anything that is not a public web address."""

import ipaddress
import re
import socket
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

USER_AGENT = "HelloDaylight/0.1 (+https://github.com/millerandmuller/hello_daylight)"
TIMEOUT = httpx.Timeout(8.0, connect=4.0)
MAX_BYTES = 2_000_000
MAX_REDIRECTS = 5
MAX_TEXT_CHARS = 8000

_DATE_PATTERNS = [
    r"\b20\d\d-\d\d-\d\d\b",
    r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.? \d{1,2},? 20\d\d\b",
    r"\b\d{1,2} (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]* 20\d\d\b",
]
_PRICE_RE = re.compile(r"(\$|€|£)\s?\d|\bpricing\b|\bper month\b|/mo\b|\bfree trial\b", re.I)


class FetchError(Exception):
    """The page could not be read. `reason` is shown to the user."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class PageSnapshot:
    url: str
    final_url: str
    title: str = ""
    description: str = ""
    headings: list[str] = field(default_factory=list)
    text: str = ""
    dates_found: list[str] = field(default_factory=list)
    mentions_pricing: bool = False
    last_modified: str | None = None

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def normalize_url(raw: str) -> str:
    url = (raw or "").strip()
    if not url:
        raise FetchError("Paste a link first.")
    if len(url) > 2000:
        raise FetchError("That link is too long.")
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        # "mailto:x", "javascript:x" carry a scheme; "example.com:8080/x" is a host with a port.
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:(?!\d)", url):
            raise FetchError("Only http and https links work here.")
        url = "https://" + url
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise FetchError("Only http and https links work here.")
    if not parsed.hostname:
        raise FetchError("That does not look like a web address.")
    return url


def _check_public_host(url: str) -> None:
    host = urlparse(url).hostname or ""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise FetchError("I could not find that address.")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global:
            raise FetchError("That address is not a public website.")


def fetch_page(raw_url: str, client: httpx.Client | None = None) -> PageSnapshot:
    url = normalize_url(raw_url)
    own_client = client is None
    client = client or httpx.Client(
        timeout=TIMEOUT, follow_redirects=False, headers={"User-Agent": USER_AGENT}
    )
    try:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            _check_public_host(current)
            try:
                with client.stream("GET", current) as resp:
                    if resp.is_redirect:
                        location = resp.headers.get("location")
                        if not location:
                            raise FetchError("The page redirected without a target.")
                        current = normalize_url(urljoin(current, location))
                        continue
                    if resp.status_code >= 400:
                        raise FetchError(f"The page answered with error {resp.status_code}.")
                    ctype = resp.headers.get("content-type", "")
                    if "html" not in ctype and "text/plain" not in ctype:
                        raise FetchError("That link is not a web page I can read.")
                    body = bytearray()
                    for chunk in resp.iter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_BYTES:
                            break
                    html = body.decode(resp.encoding or "utf-8", errors="replace")
                    return extract(url, str(resp.url), html, resp.headers.get("last-modified"))
            except httpx.TimeoutException:
                raise FetchError("The page took too long to answer.")
            except httpx.HTTPError:
                raise FetchError("I could not reach that page.")
        raise FetchError("The page redirected too many times.")
    finally:
        if own_client:
            client.close()


def extract(url: str, final_url: str, html: str, last_modified: str | None = None) -> PageSnapshot:
    soup = BeautifulSoup(html, "html.parser")

    def meta(*names: str) -> str:
        for name in names:
            tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
            if tag and tag.get("content"):
                return tag["content"].strip()
        return ""

    title = meta("og:title") or (soup.title.string.strip() if soup.title and soup.title.string else "")
    description = meta("og:description", "description")
    for tag in soup(["script", "style", "noscript", "svg", "template", "iframe"]):
        tag.decompose()
    headings = [h.get_text(" ", strip=True) for h in soup.find_all(["h1", "h2", "h3"])]
    headings = [h for h in headings if h][:25]
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))[:MAX_TEXT_CHARS]

    dates: list[str] = []
    for pattern in _DATE_PATTERNS:
        dates.extend(re.findall(pattern, text))
    return PageSnapshot(
        url=url,
        final_url=final_url,
        title=title[:300],
        description=description[:600],
        headings=headings,
        text=text,
        dates_found=list(dict.fromkeys(dates))[:10],
        mentions_pricing=bool(_PRICE_RE.search(text)),
        last_modified=last_modified,
    )


def is_readable(page: PageSnapshot) -> bool:
    """A page with almost no words (JS-only shells, parked domains) is not enough to work from."""
    return len(page.text) >= 200 or len(page.description) >= 60

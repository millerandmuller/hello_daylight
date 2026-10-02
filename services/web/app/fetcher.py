"""Reads a public project page. Refuses anything that is not a public web address."""

import asyncio
import ipaddress
import re
import socket
import zlib
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

USER_AGENT = "HelloDaylight/0.1 (+https://github.com/millerandmuller/hello_daylight)"
TIMEOUT = httpx.Timeout(6.0, connect=4.0)
TOTAL_BUDGET_S = 10.0  # hard cap on the whole fetch; + 12 s model timeout keeps intake under 30 s
MAX_BYTES = 1_000_000
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
    if any(c in url for c in "\r\n\t"):
        raise FetchError("That does not look like a web address.")
    url = url.replace(" ", "%20")  # a pasted path with a space is still a valid link
    try:
        parsed = urlparse(url)
        host = parsed.hostname
        parsed.port  # raises on "example.com:abc"
    except ValueError:
        raise FetchError("That does not look like a web address.")
    if parsed.scheme not in ("http", "https"):
        raise FetchError("Only http and https links work here.")
    if not host or ".." in host or host.startswith(".") or any(len(label) > 63 for label in host.split(".")):
        raise FetchError("That does not look like a web address.")
    return url


async def _check_public_host(url: str) -> None:
    host = urlparse(url).hostname or ""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None)
    except socket.gaierror:
        raise FetchError("I could not find that address.")
    except (UnicodeError, ValueError):
        raise FetchError("That does not look like a web address.")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global:
            raise FetchError("That address is not a public website.")


async def fetch_page_async(raw_url: str) -> PageSnapshot:
    """Hard deadline over everything: DNS, connect, headers, body, every redirect hop.

    Per-read timeouts alone do not bound a server that drips one byte at a time;
    the outer wait_for cancels the request wherever it is stuck and closes the socket.
    """
    url = normalize_url(raw_url)
    try:
        return await asyncio.wait_for(_fetch(url), timeout=TOTAL_BUDGET_S)
    except (asyncio.TimeoutError, TimeoutError):
        raise FetchError("The page took too long to answer.")


def fetch_page(raw_url: str) -> PageSnapshot:
    """Sync entry point for scripts and tests."""
    return asyncio.run(fetch_page_async(raw_url))


async def _fetch(url: str) -> PageSnapshot:
    async with httpx.AsyncClient(
        timeout=TIMEOUT,
        follow_redirects=False,
        trust_env=False,
        # identity: read raw bytes, so a compressed bomb cannot expand past MAX_BYTES
        headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"},
    ) as client:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            await _check_public_host(current)
            try:
                async with client.stream("GET", current) as resp:
                    if resp.is_redirect:
                        location = resp.headers.get("location")
                        if not location:
                            raise FetchError("The page redirected without a target.")
                        current = normalize_url(urljoin(current, location))
                        continue
                    if resp.status_code >= 400:
                        raise FetchError(f"The page answered with error {resp.status_code}.")
                    ctype = resp.headers.get("content-type", "")
                    if ctype and "html" not in ctype and "text/plain" not in ctype:
                        raise FetchError("That link is not a web page I can read.")
                    body = bytearray()
                    async for chunk in resp.aiter_raw():
                        body.extend(chunk)
                        if len(body) > MAX_BYTES:
                            break
                    raw = _decompress(bytes(body), resp.headers.get("content-encoding", ""))
                    html = raw.decode(resp.encoding or "utf-8", errors="replace")
                    return await asyncio.to_thread(extract, url, str(resp.url), html, resp.headers.get("last-modified"))
            except httpx.TimeoutException:
                raise FetchError("The page took too long to answer.")
            except (httpx.HTTPError, httpx.InvalidURL, httpx.StreamError):
                raise FetchError("I could not reach that page.")
            except (UnicodeError, ValueError):
                raise FetchError("That does not look like a web address.")
        raise FetchError("The page redirected too many times.")


def _decompress(body: bytes, encoding: str) -> bytes:
    """Some servers compress even when asked not to. Expand at most MAX_BYTES."""
    encoding = encoding.strip().lower()
    if encoding in ("", "identity"):
        return body
    if encoding in ("gzip", "x-gzip", "deflate"):
        try:
            return zlib.decompressobj(zlib.MAX_WBITS | 32).decompress(body, MAX_BYTES)
        except zlib.error:
            pass
    raise FetchError("I could not read that page's format.")


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

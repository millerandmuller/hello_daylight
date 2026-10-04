"""Read-only access to the public web for the tools and the verification step.

GET only. Public addresses only (the same guard as the intake: no private ranges, no redirects into them).
Nothing here can send anything anywhere: there is no POST, no form submit, no mail.
"""

import asyncio
import json
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx

from . import fetcher

UA = fetcher.USER_AGENT
TIMEOUT = httpx.Timeout(8.0, connect=4.0)
MAX_BYTES = 600_000
MAX_REDIRECTS = 5


class WebError(Exception):
    def __init__(self, reason: str, status: int | None = None):
        super().__init__(reason)
        self.reason = reason
        self.status = status


@dataclass
class Fetched:
    status: int
    final_url: str
    body: bytes
    content_type: str


class SafeHttp:
    """One shared client per run. `transport` is only for tests."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None, headers: dict | None = None):
        self._client = httpx.AsyncClient(
            timeout=TIMEOUT,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
            headers={"User-Agent": UA, "Accept-Encoding": "identity", **(headers or {})},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def get(self, url: str, *, params: dict | None = None, headers: dict | None = None, max_bytes: int = MAX_BYTES) -> Fetched:
        try:
            current = fetcher.normalize_url(url) if params is None else url
        except fetcher.FetchError as exc:
            raise WebError(exc.reason)
        for hop in range(MAX_REDIRECTS + 1):
            try:
                await fetcher._check_public_host(current)
            except fetcher.FetchError as exc:  # a private or dead address is a dropped finding, never a failed night
                raise WebError(exc.reason)
            try:
                async with self._client.stream("GET", current, params=params if hop == 0 else None, headers=headers) as resp:
                    if resp.is_redirect:
                        loc = resp.headers.get("location")
                        if not loc:
                            raise WebError("redirect without target", resp.status_code)
                        current = fetcher.normalize_url(urljoin(str(resp.url), loc))
                        continue
                    body = bytearray()
                    try:
                        async for chunk in resp.aiter_raw():
                            body.extend(chunk)
                            if len(body) > max_bytes:
                                break
                    except httpx.StreamConsumed:  # a response that is already in memory (test transports)
                        body = bytearray(resp.content[: max_bytes + 1])
                    raw = fetcher._decompress(bytes(body), resp.headers.get("content-encoding", ""))
                    return Fetched(resp.status_code, str(resp.url), raw, resp.headers.get("content-type", ""))
            except httpx.TimeoutException:
                raise WebError("timeout")
            except (httpx.HTTPError, httpx.InvalidURL, httpx.StreamError):
                raise WebError("unreachable")
            except fetcher.FetchError as exc:
                raise WebError(exc.reason)
        raise WebError("too many redirects")

    async def get_json(self, url: str, *, params: dict | None = None, headers: dict | None = None):
        res = await self.get(url, params=params, headers=headers)
        if res.status >= 400:
            raise WebError(f"http {res.status}", res.status)
        try:
            return json.loads(res.body.decode("utf-8", errors="replace"))
        except ValueError:
            raise WebError("not json")

    async def check_link(self, url: str) -> tuple[int, str]:
        """(status, final_url) of a plain GET with the body cut short. 0 when it cannot be reached."""
        try:
            res = await asyncio.wait_for(self.get(url, max_bytes=2048), timeout=12)
            return res.status, res.final_url
        except (WebError, asyncio.TimeoutError):
            return 0, url

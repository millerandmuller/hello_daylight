"""Checks a visitor's Gemini key with a call that costs nothing (listing one model), before any paid call."""

import httpx

URL = "https://generativelanguage.googleapis.com/v1beta/models"


async def check_key(key: str, *, transport: httpx.AsyncBaseTransport | None = None) -> bool | None:
    """True: valid. False: refused by Google. None: could not be checked (no answer, quota, outage)."""
    if not key or len(key) > 200 or not key.isascii() or any(c.isspace() for c in key):
        return False
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(6.0, connect=4.0), trust_env=False, transport=transport) as client:
            resp = await client.get(URL, params={"pageSize": 1}, headers={"x-goog-api-key": key})
    except httpx.HTTPError:
        return None
    if resp.status_code == 200:
        return True
    if resp.status_code in (400, 401, 403):
        return False
    return None

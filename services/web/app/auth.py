"""Who may use the private mode: Google sign-in through Firebase Authentication, plus the allowlist.

Order of the checks on every request that can cost money or show private data:
  no valid sign-in -> 401, signed in but not on the allowlist -> 403. The allowlist is read on every request, so
removing someone takes effect at once. The session cookie only proves who someone is, never that they may enter.
"""

import base64
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass

from fastapi import HTTPException, Request

from . import config
from .repo import Repo

log = logging.getLogger("daylight.auth")
COOKIE = "daylight_session"


@dataclass
class User:
    email: str
    uid: str
    is_admin: bool


class AuthError(HTTPException):
    pass


def assert_safe_config() -> None:
    """Refuse to start with the development login in the cloud, or with no session secret in the cloud."""
    if config.AUTH_MODE == "dev" and config.IS_CLOUD:
        raise RuntimeError("DAYLIGHT_AUTH_MODE=dev is not allowed on Cloud Run")
    if config.AUTH_MODE not in ("dev", "firebase"):
        raise RuntimeError("DAYLIGHT_AUTH_MODE must be 'firebase' or 'dev'")
    if config.IS_CLOUD and len(config.SESSION_SECRET) < 32:
        raise RuntimeError("DAYLIGHT_SESSION_SECRET (32+ characters) is required on Cloud Run")


def _secret() -> bytes:
    return (config.SESSION_SECRET or "local-development-only-secret").encode()


def make_session(email: str, uid: str, now: float | None = None) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"e": email, "u": uid, "x": int((now or time.time()) + config.SESSION_HOURS * 3600)}).encode()).decode()
    sig = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def read_session(value: str, now: float | None = None) -> tuple[str, str] | None:
    try:
        payload, sig = value.rsplit(".", 1)
        good = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, good):
            return None
        data = json.loads(base64.urlsafe_b64decode(payload.encode()))
        if data["x"] < (now or time.time()):
            return None
        return data["e"], data["u"]
    except (ValueError, KeyError, TypeError):
        return None


def verify_id_token(token: str) -> tuple[str, str]:
    """(email, uid) of a valid sign-in token, or raise AuthError(401)."""
    if config.AUTH_MODE == "dev":
        if config.IS_CLOUD or not token.startswith("dev:") or "@" not in token:
            raise AuthError(401, "Sign in first.")
        email = token[4:].strip().lower()
        return email, "dev-" + hashlib.sha256(email.encode()).hexdigest()[:16]
    try:
        from google.auth.transport import requests as g_requests
        from google.oauth2 import id_token

        claims = id_token.verify_firebase_token(token, g_requests.Request(), audience=config.FIREBASE_PROJECT_ID)
    except Exception as exc:  # noqa: BLE001 - expired, forged, wrong project: all the same to the caller
        log.info("sign-in token rejected: %s", type(exc).__name__)
        raise AuthError(401, "Sign in first.")
    email = (claims.get("email") or "").lower()
    if not email or not claims.get("email_verified"):
        raise AuthError(401, "Sign in first.")
    return email, claims["sub"]


def _token_from(request: Request) -> tuple[str | None, bool]:
    """(token or session cookie value, came_from_cookie)."""
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip(), False
    cookie = request.cookies.get(COOKIE)
    return (cookie, True) if cookie else (None, False)


def check_same_origin(request: Request) -> None:
    """A cookie-authenticated change must come from this site (no cross-site form posts)."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    site = request.headers.get("sec-fetch-site")
    if site in ("same-origin", "none"):
        return
    origin = request.headers.get("origin")
    if origin:
        host = request.headers.get("host", "")
        if origin.split("://", 1)[-1] == host:
            return
        raise AuthError(403, "Cross-site request refused.")
    if site is None and not origin:
        return  # non-browser client; a browser always sends one of the two headers
    raise AuthError(403, "Cross-site request refused.")


def current_user(request: Request, repo: Repo) -> User:
    token, from_cookie = _token_from(request)
    if not token:
        raise AuthError(401, "Sign in first.")
    if from_cookie:
        check_same_origin(request)
        session = read_session(token)
        if not session:
            raise AuthError(401, "Sign in first.")
        email, uid = session
    else:
        email, uid = verify_id_token(token)
    is_admin = email in config.ADMIN_EMAILS
    if not is_admin and repo.allow_get(email) is None:
        raise AuthError(403, "This account is not on the list.")
    return User(email=email, uid=uid, is_admin=is_admin)


def require_admin(user: User) -> User:
    if not user.is_admin:
        raise AuthError(403, "Only the operator may do this.")
    return user

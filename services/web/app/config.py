"""The one config place: models, prices, the run contract, storage and auth switches.

Everything an operator may want to change lives here or in environment variables.
Nothing in this repo is specific to one operator: no keys, no allowlist, no project ids.
"""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[3]

# Local development reads the key from the repo-root .env (gitignored).
# On Cloud Run the same variables come from Secret Manager.
load_dotenv(REPO_ROOT / ".env")


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    return int(_float(name, default))


IS_CLOUD = bool(os.getenv("K_SERVICE") or os.getenv("CLOUD_RUN_JOB"))

# --- models ---------------------------------------------------------------------------------
# Three tiers, one place. Switching a tier is a one-line env change.
TIERS = {
    "cheap": os.getenv("DAYLIGHT_MODEL_CHEAP", "gemini-3.5-flash-lite"),
    "mid": os.getenv("DAYLIGHT_MODEL_MID", "gemini-3.8-flash"),
    "strong": os.getenv("DAYLIGHT_MODEL_STRONG", "gemini-3.1-pro-preview"),
}
MODEL = os.getenv("DAYLIGHT_INTAKE_MODEL", TIERS["mid"])  # intake: one call on the mid tier

# Timeout of ONE model call in milliseconds. Set together with the retry in one http_options object.
CALL_TIMEOUT_MS = {
    "cheap": _int("DAYLIGHT_TIMEOUT_CHEAP_MS", 60_000),
    "mid": _int("DAYLIGHT_TIMEOUT_MID_MS", 90_000),
    "strong": _int("DAYLIGHT_TIMEOUT_STRONG_MS", 150_000),
}
REQUEST_RETRY_ATTEMPTS = 3  # google-genai request-level retry on 429/5xx codes that come back

# Output caps per call. They also bound what the budget reserves before a call.
MAX_OUTPUT_TOKENS = {"cheap": 3_000, "mid": 4_000, "strong": 4_096}  # the plan is about 2,500 tokens; the cap also sets what a 0.05 EUR budget can still afford

# --- prices ---------------------------------------------------------------------------------
# EUR per million tokens, standard paid tier, prompts up to 200k tokens.
# Source: https://ai.google.dev/gemini-api/docs/pricing, checked 2026-10-03,
# converted at 1 EUR = 1.1225 USD (ECB reference rate 2026-10-02).
# gemini-3.8-flash doubles in price on 2027-01-01 (1.50 / 7.50 USD): update the line below then.
PRICES_EUR_PER_MTOK = {
    "gemini-3.5-flash-lite": (0.2673, 2.2272),
    "gemini-3.8-flash": (0.6682, 3.3408),
    "gemini-3.1-pro-preview": (1.7817, 10.6904),
}
_UNKNOWN_MODEL_PRICE = (1.7817, 10.6904)  # unknown model: assume the strongest tier, never the cheapest
# Google Search grounding: 14 USD per 1,000 queries after the free allowance. The ledger assumes no allowance.
SEARCH_EUR_PER_QUERY = 14 / 1.1225 / 1000


def price_for(model: str) -> tuple[float, float]:
    return PRICES_EUR_PER_MTOK.get(model, _UNKNOWN_MODEL_PRICE)


# --- the run contract ---------------------------------------------------------------------
@dataclass(frozen=True)
class RunContract:
    mode: str  # "private" or "public"
    max_steps: int  # model calls per run, hard
    max_retries: int  # retries of failed calls AND replaced sub-agents together, hard
    budget_eur: float  # per run; the kill switch fires when a call would cross it
    min_tasks: int
    max_tasks: int
    max_replacements_run: int
    max_replacements_task: int
    scout_concurrency: int = 6
    scout_deadline_s: float = 150.0  # one sub-agent may take this long, then it ends with what it has
    max_search_queries: int = 24  # Google Search grounding queries per run; each one is billed (the largest single cost)
    max_cards: int = 5
    max_rounds_write: int = 2
    lock_ttl_s: float = 90.0


PRIVATE_BUDGET_CEILING_EUR = _float("DAYLIGHT_BUDGET_CEILING_EUR", 1.50)
PUBLIC_BUDGET_CEILING_EUR = _float("DAYLIGHT_PUBLIC_BUDGET_CEILING_EUR", 1.00)
MONTH_CAP_PER_WORKSPACE_EUR = _float("DAYLIGHT_MONTH_CAP_EUR", 25.0)
GLOBAL_MONTH_CAP_EUR = _float("DAYLIGHT_GLOBAL_MONTH_CAP_EUR", 60.0)
MAX_NEW_PROJECTS_PER_DAY = _int("DAYLIGHT_MAX_NEW_PROJECTS_PER_DAY", 3)


def contract_for(mode: str, budget_override: float | None = None) -> RunContract:
    """The contract of one run. A per-run budget can only be lowered, never raised, without editing code.

    Lower it with the environment variable DAYLIGHT_RUN_BUDGET_EUR (every mode) or with a run parameter
    (`--budget` on the command line, the budget field of the public run, the budget input on the run page).
    """
    if mode == "public":
        ceiling = PUBLIC_BUDGET_CEILING_EUR
        shape = dict(min_tasks=4, max_tasks=8, max_replacements_run=2, max_replacements_task=1, max_search_queries=_int("DAYLIGHT_PUBLIC_MAX_SEARCH_QUERIES", 10))
    else:
        ceiling = PRIVATE_BUDGET_CEILING_EUR
        shape = dict(min_tasks=8, max_tasks=18, max_replacements_run=4, max_replacements_task=2, max_search_queries=_int("DAYLIGHT_MAX_SEARCH_QUERIES", 24))
    candidates = [ceiling]
    env_budget = os.getenv("DAYLIGHT_RUN_BUDGET_EUR")
    if env_budget not in (None, ""):
        try:
            candidates.append(float(env_budget.replace(",", ".")))
        except ValueError:
            candidates.append(0.0)  # an unreadable budget must not mean "the full budget": it means none
    if budget_override is not None:
        candidates.append(budget_override)
    budget = max(0.0, min(candidates))
    return RunContract(
        mode=mode,
        max_steps=_int("DAYLIGHT_MAX_STEPS", 220),
        max_retries=_int("DAYLIGHT_MAX_RETRIES", 16),
        budget_eur=budget,
        scout_deadline_s=_float("DAYLIGHT_SCOUT_DEADLINE_S", 150.0),
        lock_ttl_s=_float("DAYLIGHT_LOCK_TTL_S", 90.0),
        **shape,
    )


# --- storage, auth, launching ---------------------------------------------------------------
DATA_DIR = Path(os.getenv("DAYLIGHT_DATA_DIR", REPO_ROOT / ".data"))
BACKEND = os.getenv("DAYLIGHT_BACKEND", "firestore" if IS_CLOUD else "file")  # "file" | "firestore"
LEDGER_FILE = Path(os.getenv("DAYLIGHT_LEDGER_FILE", DATA_DIR / "ledger.jsonl"))

# "firebase": real Google sign-in. "dev": tokens of the form dev:<email>, only ever allowed outside the cloud.
AUTH_MODE = os.getenv("DAYLIGHT_AUTH_MODE", "firebase")
FIREBASE_PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID") or os.getenv("GOOGLE_CLOUD_PROJECT") or ""
FIREBASE_WEB_CONFIG = {
    "apiKey": os.getenv("FIREBASE_WEB_API_KEY", ""),
    "authDomain": os.getenv("FIREBASE_AUTH_DOMAIN", ""),
    "projectId": FIREBASE_PROJECT_ID,
}
ADMIN_EMAILS = {e.strip().lower() for e in os.getenv("DAYLIGHT_ADMIN_EMAILS", "").split(",") if e.strip()}
SESSION_SECRET = os.getenv("DAYLIGHT_SESSION_SECRET", "")
SESSION_HOURS = 12

LAUNCHER = os.getenv("DAYLIGHT_LAUNCHER", "subprocess")  # "subprocess" | "cloudrun"
CLOUD_RUN_JOB = os.getenv("DAYLIGHT_CLOUD_RUN_JOB", "")  # full name: projects/<p>/locations/<r>/jobs/<name>

# Public mode
PUBLIC_MAX_CONCURRENT = _int("DAYLIGHT_PUBLIC_MAX_CONCURRENT", 3)
PUBLIC_RATE_PER_IP_PER_HOUR = _int("DAYLIGHT_PUBLIC_RATE_PER_IP_PER_HOUR", 6)
PUBLIC_MAX_BODY_BYTES = 60_000
GEMINI_API_KEY_ENV = ("GEMINI_API_KEY", "GOOGLE_API_KEY")


def operator_key() -> str | None:
    """The operator's key for the private mode. Never used for a public run."""
    for name in GEMINI_API_KEY_ENV:
        value = os.getenv(name)
        if value:
            return value
    return None


REPO_URL = "https://github.com/millerandmuller/hello_daylight"
NIGHT_TIMEZONE = "America/Toronto"

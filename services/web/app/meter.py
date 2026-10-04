"""Counts steps, retries and money for ONE run and fires the kill switch.

The meter is the single source of truth for the run contract:
  - max_steps      model calls per run
  - max_retries    retries of failed calls plus replaced sub-agents
  - budget_eur     per-run budget; a call that would cross it is refused BEFORE it is made
It also feeds the ledger line. Its state survives an abort (checkpoint), so a resumed run never forgets
what was already spent.
"""

import threading
from dataclasses import dataclass, field

from . import config


class RunStopped(Exception):
    """The run must end now. `reason` is one of: budget, max_steps, max_retries, cancelled."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


REASON_TEXT = {
    "budget": "Run stopped: budget reached.",
    "max_steps": "Run stopped: step limit reached.",
    "max_retries": "Run stopped: retry limit reached.",
    "cancelled": "Run stopped: cancelled.",
}


@dataclass
class StepStat:
    model: str = ""
    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_eur: float = 0.0
    searches: int = 0

    def as_dict(self) -> dict:
        return {
            "model": self.model,
            "calls": self.calls,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "searches": self.searches,
            "cost_eur": round(self.cost_eur, 5),
        }


def estimate_tokens(chars: int) -> int:
    """Rough input size of a request, on the pessimistic side (German and code run near 3 chars per token)."""
    return int(chars / 3.0) + 50


class RunMeter:
    def __init__(self, contract: config.RunContract, state: dict | None = None):
        self.contract = contract
        self._lock = threading.Lock()
        self.steps: dict[str, StepStat] = {}
        self.calls = 0
        self.retries = 0
        self.replacements = 0
        self.search_queries = 0
        self.search_reserved = 0
        self.spent_eur = 0.0
        self.reserved_eur = 0.0
        self.stopped: str | None = None
        self.stopped_detail = ""
        self.cancelled = False
        if state:
            self._load(state)

    # --- checkpoint --------------------------------------------------------------------------
    def state(self) -> dict:
        with self._lock:
            return {
                "steps": {k: v.as_dict() for k, v in self.steps.items()},
                "calls": self.calls,
                "retries": self.retries,
                "replacements": self.replacements,
                "search_queries": self.search_queries,
                "spent_eur": round(self.spent_eur, 6),
            }

    def _load(self, state: dict) -> None:
        for name, s in (state.get("steps") or {}).items():
            self.steps[name] = StepStat(
                model=s.get("model", ""),
                calls=s.get("calls", 0),
                tokens_in=s.get("tokens_in", 0),
                tokens_out=s.get("tokens_out", 0),
                cost_eur=s.get("cost_eur", 0.0),
                searches=s.get("searches", 0),
            )
        self.calls = int(state.get("calls", 0))
        self.retries = int(state.get("retries", 0))
        self.replacements = int(state.get("replacements", 0))
        self.search_queries = int(state.get("search_queries", 0))
        self.spent_eur = float(state.get("spent_eur", 0.0))

    # --- the gates ---------------------------------------------------------------------------
    def stop(self, reason: str, detail: str = "") -> None:
        with self._lock:
            if self.stopped is None:
                self.stopped, self.stopped_detail = reason, detail

    def check_alive(self) -> None:
        if self.cancelled:
            self.stop("cancelled")
        if self.stopped:
            raise RunStopped(self.stopped, self.stopped_detail)

    def est_cost(self, tier: str, model: str, chars_in: int, step: str = "") -> float:
        price_in, price_out = config.price_for(model)
        fee = 3 * config.SEARCH_EUR_PER_QUERY if step == "search" else 0.0  # a grounded call may fire up to ~3 billed queries
        return (
            estimate_tokens(chars_in) * price_in + config.MAX_OUTPUT_TOKENS.get(tier, 4000) * price_out
        ) / 1_000_000 + fee

    def before_call(self, step: str, tier: str, model: str, chars_in: int) -> float:
        """Reserve the worst-case cost of one call. Raises RunStopped instead of letting the call happen."""
        self.check_alive()
        est = self.est_cost(tier, model, chars_in, step)
        with self._lock:
            if self.calls >= self.contract.max_steps:
                self.stopped, self.stopped_detail = "max_steps", f"{self.calls} calls"
            elif self.spent_eur + self.reserved_eur + est > self.contract.budget_eur:
                self.stopped = "budget"
                self.stopped_detail = (
                    f"spent {self.spent_eur:.4f} EUR, next call may cost {est:.4f} EUR, "
                    f"limit {self.contract.budget_eur:.2f} EUR"
                )
            if self.stopped:
                raise RunStopped(self.stopped, self.stopped_detail)
            self.reserved_eur += est
            self.calls += 1
            stat = self.steps.setdefault(step, StepStat(model=model))
            stat.calls += 1
            stat.model = model
        return est

    def record(self, step: str, model: str, reserved: float, tokens_in: int, tokens_out: int, searches: int = 0) -> float:
        """Book the real cost of a finished call and release its reservation."""
        price_in, price_out = config.price_for(model)
        cost = (tokens_in * price_in + tokens_out * price_out) / 1_000_000 + searches * config.SEARCH_EUR_PER_QUERY
        with self._lock:
            self.reserved_eur = max(0.0, self.reserved_eur - reserved)
            self.spent_eur += cost
            self.search_queries += searches
            stat = self.steps.setdefault(step, StepStat(model=model))
            stat.tokens_in += tokens_in
            stat.tokens_out += tokens_out
            stat.cost_eur += cost
            stat.searches += searches
            if self.spent_eur >= self.contract.budget_eur and not self.stopped:
                self.stopped = "budget"
                self.stopped_detail = f"spent {self.spent_eur:.4f} EUR, limit {self.contract.budget_eur:.2f} EUR"
        return cost

    def release(self, reserved: float) -> None:
        with self._lock:
            self.reserved_eur = max(0.0, self.reserved_eur - reserved)

    def reserve_search(self, n: int = 3) -> bool:
        """Claim room for the queries of one grounded call (a call may fire up to ~3). False when the allowance is gone."""
        with self._lock:
            if self.search_queries + self.search_reserved >= self.contract.max_search_queries:
                return False
            self.search_reserved += n
            return True

    def release_search(self, n: int = 3) -> None:
        with self._lock:
            self.search_reserved = max(0, self.search_reserved - n)

    def add_search(self, step: str, n: int) -> None:
        """Grounding queries found after the fact (the fee is billed per query)."""
        if n <= 0:
            return
        with self._lock:
            cost = n * config.SEARCH_EUR_PER_QUERY
            self.spent_eur += cost
            self.search_queries += n
            stat = self.steps.setdefault(step, StepStat())
            stat.searches += n
            stat.cost_eur += cost

    def count_retry(self, what: str, replacement: bool = False) -> None:
        with self._lock:
            if self.retries >= self.contract.max_retries:
                self.stopped, self.stopped_detail = "max_retries", what
                raise RunStopped(self.stopped, self.stopped_detail)
            self.retries += 1
            if replacement:
                self.replacements += 1

    # --- reporting ---------------------------------------------------------------------------
    @property
    def total_calls(self) -> int:
        return self.calls

    def summary(self) -> dict:
        with self._lock:
            return {
                "steps": {k: v.as_dict() for k, v in self.steps.items()},
                "calls": self.calls,
                "retries": self.retries,
                "replacements": self.replacements,
                "search_queries": self.search_queries,
                "cost_eur": round(self.spent_eur, 4),
                "budget_eur": self.contract.budget_eur,
            }

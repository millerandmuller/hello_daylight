"""The entry point of the night job (Cloud Run Job) and of a run by hand.

    python -m app.night                       every workspace with confirmed goals, one after the other
    python -m app.night --project <id>        one workspace (a run on click uses this)
    python -m app.night --project <id> --budget 0.05    lower this run's budget, no file edited

Starting twice is safe: a second start of the same workspace is refused while the first one is alive, and a run
that died is resumed from its checkpoint.

Exit codes: a refusal is a decision, not a failure, so it exits 0. Only a run that failed exits 1. If the platform
retries the job anyway (Cloud Run sets CLOUD_RUN_TASK_ATTEMPT > 0), the retry may only resume an unfinished run of the
same workspace; it never starts a fresh, paid one.
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

from . import config, logsafe
from .repo import open_repo, parse_ts
from .service import RunRefused, month_room, run_private

log = logging.getLogger("daylight.night")
SKIP_IF_RAN_WITHIN_H = 18


def _recent_run(repo, project_id: str) -> bool:
    for run in repo.list_runs(project_id, 3):
        started = parse_ts(run.get("started_at"))
        if started and (run.get("status") in ("running", "ok") or (run.get("status") == "partial" and run.get("cards"))) and datetime.now(timezone.utc) - started < timedelta(hours=SKIP_IF_RAN_WITHIN_H):
            return True
    return False


def platform_retry() -> bool:
    """True when this process is a retry the platform started on its own, not a start somebody asked for."""
    try:
        return int(os.environ.get("CLOUD_RUN_TASK_ATTEMPT", "0") or 0) > 0
    except ValueError:
        return False


async def night(args) -> int:
    repo = open_repo()
    resume_only = platform_retry()
    if resume_only:
        log.warning("platform retry: this attempt may only resume an unfinished run, never start a new one")
    projects = [repo.get_project(args.project)] if args.project else [p for p in repo.list_projects() if p.get("confirmed_goals") is not None and p.get("nightly", True)]
    code = 0
    for project in projects:
        pid = project["id"]
        if not args.project and not args.force and _recent_run(repo, pid):
            print(json.dumps({"project_id": pid, "skipped": "ran within the last 18 hours"}))
            continue
        try:
            ledger = await run_private(
                repo, pid, trigger=args.trigger, budget_override=args.budget, wait_stale=args.wait_stale, takeover=args.takeover, resume_only=resume_only
            )
        except RunRefused as exc:
            print(json.dumps({"project_id": pid, "refused": exc.reason, "message": exc.message}))
            if exc.reason in ("global_cap", "no_key"):
                break  # every further run would be refused for the same reason
            continue  # a refusal exits 0: a non-zero exit would make the platform retry, and a retry must never pay twice
        print(json.dumps({k: ledger[k] for k in ("run_id", "project_id", "status", "reason", "cost_eur", "calls", "cards")}))
        if ledger["status"] in ("failed",):
            code = 1
    return code


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.night")
    ap.add_argument("--project", help="one workspace id; without it every confirmed workspace runs")
    ap.add_argument("--trigger", choices=("schedule", "manual"), default="schedule")
    ap.add_argument("--budget", type=float, help="lower the per-run budget (EUR) for this run only")
    ap.add_argument("--takeover", action="store_true", help="take over a lock whose owner is dead, without waiting for it to go stale")
    ap.add_argument("--wait-stale", action="store_true", help="wait (without calling a model) for a live-looking lock to go stale, then resume")
    ap.add_argument("--force", action="store_true", help="run even if the workspace already ran in the last 18 hours")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logsafe.install()
    return asyncio.run(night(args))


if __name__ == "__main__":
    sys.exit(main())

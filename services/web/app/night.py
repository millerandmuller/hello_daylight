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
from .repo import now, open_repo, parse_ts
from .service import RunRefused, month_room, run_private

log = logging.getLogger("daylight.night")
SKIP_IF_RAN_WITHIN_H = 18


def _recent_run(repo, project_id: str) -> bool:
    """Does an earlier start already cover this night? A run that is still going counts, whatever started it. Of the finished ones
    only a scheduled night counts: a run on click in the afternoon must not swallow the night."""
    for run in repo.list_runs(project_id, 3):
        started = parse_ts(run.get("started_at"))
        if not started or datetime.now(timezone.utc) - started >= timedelta(hours=SKIP_IF_RAN_WITHIN_H):
            continue
        if run.get("status") == "running":
            return True
        if run.get("trigger") == "schedule" and (run.get("status") == "ok" or (run.get("status") == "partial" and run.get("cards"))):
            return True
    return False


# Refusals that are not an outage of the night: a start that is already covered by a run in progress, or a platform retry.
QUIET_REFUSALS = ("already_running", "nothing_to_resume")


def note_missed_night(repo, project_id: str, reason: str) -> None:
    """One line for the morning desk: why last night did not run. Written by code, no model call, no ledger line (there was no run).
    The next run of this workspace removes it."""
    reason = " ".join(reason.split()).rstrip(".")
    try:
        repo.update_project(project_id, lambda d: d.update(night_note={"reason": reason, "at": now()}))
    except Exception:  # noqa: BLE001 - the note is a courtesy; failing to write it must never stop the other workspaces
        log.warning("could not write the missed-night note for %s", project_id)


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
    scheduled = args.trigger == "schedule"
    if args.project:
        projects = [repo.get_project(args.project)]
    else:
        confirmed = [p for p in repo.list_projects() if p.get("confirmed_goals") is not None]
        if scheduled and not resume_only:
            for p in confirmed:
                if not p.get("nightly", True):
                    note_missed_night(repo, p["id"], "this workspace is paused")
        projects = [p for p in confirmed if p.get("nightly", True)]
    code = 0
    for project in projects:
        pid = project["id"]
        if not args.project and not args.force and _recent_run(repo, pid):
            print(json.dumps({"project_id": pid, "skipped": "ran within the last 18 hours"}))
            continue  # the night is covered by a run that happened: no "did not run" line for it
        try:
            ledger = await run_private(
                repo, pid, trigger=args.trigger, budget_override=args.budget, wait_stale=args.wait_stale, takeover=args.takeover, resume_only=resume_only
            )
        except RunRefused as exc:
            print(json.dumps({"project_id": pid, "refused": exc.reason, "message": exc.message}))
            if scheduled and not resume_only and exc.reason not in QUIET_REFUSALS:
                note_missed_night(repo, pid, exc.message)
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

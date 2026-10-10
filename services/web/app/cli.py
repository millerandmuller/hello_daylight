"""Operator tools on the command line: create a workspace, confirm goals, give feedback, look at a run and the ledger.

    python -m app.cli intake <url> [--description "..."] [--goal "..."]...
    python -m app.cli confirm <project-id> [--all]    confirms the goals you accepted; --all accepts every open suggestion first
    python -m app.cli feedback <project-id> <card-id> up|down [--comment "..."]
    python -m app.cli show <project-id>            the last run: crew, cards, cost line
    python -m app.cli ledger [--project <id>]      ledger lines, newest first
    python -m app.cli pause <project-id>           no more night runs for this workspace (unpause: the same with --off)
    python -m app.cli delete <project-id> --yes    remove the workspace, its runs and feedback; ledger lines stay
    python -m app.night --project <project-id>     start a run (see night.py)

The owner of a workspace made here is `cli@local` unless --owner is given.
"""

import argparse
import asyncio
import json
import sys

from . import fetcher, intake, logsafe, proposer
from .repo import now, open_repo
from .store import NoGoalsAccepted, PitchInvalid, ProjectStore


def _repo_and_store():
    repo = open_repo()
    return repo, ProjectStore(repo)


async def cmd_intake(args) -> int:
    repo, store = _repo_and_store()
    started = now()
    try:
        data, usage = await intake.run_intake(args.url, "\n".join(args.goal or []), args.description or "", fetch_page=fetcher.fetch_page_async, propose=proposer.propose)
    except intake.IntakeInvalid as exc:
        print(f"error: {exc.reason}")
        return 2
    except intake.IntakeNeedsDescription as exc:
        print(f"error: {exc.fetch_error} Give --description with one sentence.")
        return 2
    pid = store.create(args.owner, data)
    repo.append_ledger(intake.intake_ledger_line(pid, "private", usage, started))
    print(json.dumps({"project_id": pid, "card": data["card"], "pitch_line": data.get("pitch_line"), "goals": [{"id": g["id"], "text": g["text"], "status": g["status"], "reason": g["reason"]} for g in data["goals"]]}, ensure_ascii=False, indent=2))
    return 0


def cmd_confirm(args) -> int:
    _, store = _repo_and_store()
    try:
        project = store.confirm_goals(args.project, accept_all=args.all)
    except NoGoalsAccepted:
        print("error: no goal is accepted yet. Accept or write at least one, or use --all to accept every suggestion.")
        return 2
    except PitchInvalid as exc:
        print(f"error: the project sentence cannot be confirmed: {exc}")
        return 2
    print(json.dumps({"confirmed": [g["text"] for g in project["confirmed_goals"]]}, ensure_ascii=False))
    return 0


def cmd_feedback(args) -> int:
    repo, _ = _repo_and_store()
    card = None
    for run in repo.list_runs(args.project, 5):
        card = next((c for c in run.get("cards", []) if c["id"] == args.card), None)
        if card:
            break
    if not card:
        print("error: no such card in the last runs")
        return 2
    repo.add_feedback(args.project, {"kind": args.thumb, "comment": args.comment or "", "card_id": card["id"], "card_title": card["title"], "card_url": card["url"], "card_author": card["author"]})
    print("saved")
    return 0


def cmd_show(args) -> int:
    repo, _ = _repo_and_store()
    runs = repo.list_runs(args.project, 1)
    if not runs:
        print("no run yet")
        return 1
    run = runs[0]
    print(f"run {run['run_id']}  status={run['status']}  {run.get('headline') or ''}")
    if run.get("feedback_note"):
        print(f"Changed because of your feedback: {run['feedback_note']}")
    for t in run.get("plan_view", []):
        print(f"  {t['id']:>4} [{t['status']:>9}] {t['role']}  -> {t['found']} found  {('(' + t['reason'] + ')') if t['reason'] else ''}")
    for c in run.get("cards", []):
        print(f"\n--- {c['id']} [{c['kind']}] {c['title']}\n    {c['url']} ({c['date']})\n    {c['draft']}")
    print("\n" + (run.get("cost_line") or ""))
    return 0


def cmd_ledger(args) -> int:
    repo, _ = _repo_and_store()
    for line in repo.ledger(args.project, args.limit):
        print(json.dumps(line, ensure_ascii=False))
    return 0


def cmd_pause(args) -> int:
    repo, _ = _repo_and_store()
    repo.update_project(args.project, lambda d: d.update(nightly=bool(args.off)))
    print("night runs " + ("on" if args.off else "paused") + f" for {args.project}")
    return 0


def cmd_delete(args) -> int:
    repo, _ = _repo_and_store()
    project = repo.get_project(args.project)
    running = [r for r in repo.list_runs(args.project, 1) if r.get("status") == "running"]
    if running:
        print("error: a run of this workspace is in progress; cancel it first")
        return 2
    if not args.yes:
        print(f"would delete workspace {args.project} ({project.get('url', '')}, owner {project.get('owner')}); add --yes to do it")
        return 1
    print(json.dumps({"deleted": args.project, "documents": repo.delete_project(args.project)}))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("intake")
    p.add_argument("url")
    p.add_argument("--description")
    p.add_argument("--goal", action="append")
    p.add_argument("--owner", default="cli@local")
    p = sub.add_parser("confirm")
    p.add_argument("project")
    p.add_argument("--all", action="store_true", help="accept every open suggestion, then confirm")
    p = sub.add_parser("feedback")
    p.add_argument("project")
    p.add_argument("card")
    p.add_argument("thumb", choices=("up", "down"))
    p.add_argument("--comment")
    p = sub.add_parser("show")
    p.add_argument("project")
    p = sub.add_parser("pause")
    p.add_argument("project")
    p.add_argument("--off", action="store_true", help="switch night runs back on")
    p = sub.add_parser("delete")
    p.add_argument("project")
    p.add_argument("--yes", action="store_true")
    p = sub.add_parser("ledger")
    p.add_argument("--project")
    p.add_argument("--limit", type=int, default=20)
    args = ap.parse_args(argv)
    logsafe.install()
    if args.cmd == "intake":
        return asyncio.run(cmd_intake(args))
    return {"confirm": cmd_confirm, "feedback": cmd_feedback, "show": cmd_show, "ledger": cmd_ledger, "pause": cmd_pause, "delete": cmd_delete}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())

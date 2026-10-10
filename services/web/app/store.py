"""Project storage. A JSON file per project for now; the same interface moves to Firestore.

Writes are field patches on a fresh read, never a blind overwrite of someone else's change.
"""

import secrets

from .prompts import lint_pitch
from .repo import NotFound, Repo, now  # noqa: F401  (re-exported: the routes catch NotFound from here)

MAX_GOALS = 20
MAX_GOAL_CHARS = 200


def clean_goal(text: str) -> str:
    return " ".join((text or "").split())[:MAX_GOAL_CHARS]


class GoalLimit(Exception):
    pass


class NoGoalsAccepted(Exception):
    """Confirm needs at least one goal the owner accepted or wrote."""


class PitchInvalid(Exception):
    """The project sentence breaks the mechanical rules; `problems` is shown at the field, nothing is corrected silently."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


ACCEPTED = ("accepted", "edited")


def project_names(data: dict) -> list[str]:
    name = ((data.get("card") or {}).get("name") or "").strip()
    return [n for n in dict.fromkeys([name, name.split(":")[0].split(" - ")[0].strip()]) if n]


class ProjectStore:
    """Goal logic over a repo. A project is owned by one signed-in user; every call names the owner."""

    def __init__(self, repo: Repo):
        self.repo = repo

    def create(self, owner: str, data: dict) -> str:
        return self.repo.create_project(owner, data)

    def get(self, pid: str, owner: str | None = None) -> dict:
        doc = self.repo.get_project(pid)
        if owner is not None and doc.get("owner") != owner:
            raise NotFound(pid)  # someone else's project looks like no project
        return doc

    def update(self, pid: str, mutate, owner: str | None = None) -> dict:
        if owner is not None:
            self.get(pid, owner)
        return self.repo.update_project(pid, mutate)

    # --- goals ---------------------------------------------------------------

    @staticmethod
    def new_goal(text: str, origin: str, reason: str | None = None, status: str = "accepted") -> dict:
        return {
            "id": "g_" + secrets.token_hex(4),
            "text": clean_goal(text),
            "origin": origin,
            "reason": reason,
            "status": status,
            "created_at": now(),
        }

    @staticmethod
    def active_goals(data: dict) -> list[dict]:
        return [g for g in data.get("goals", []) if g["status"] != "removed"]

    def add_goal(self, token: str, text: str, owner: str | None = None) -> dict:
        text = clean_goal(text)

        def mutate(data):
            if not text:
                return
            if len(self.active_goals(data)) >= MAX_GOALS:
                raise GoalLimit()
            data.setdefault("goals", []).append(self.new_goal(text, "user"))

        return self.update(token, mutate, owner)

    def set_goal_status(self, token: str, goal_id: str, status: str, text: str | None = None, restore: bool = False, owner: str | None = None) -> dict:
        def mutate(data):
            for goal in data.get("goals", []):
                if goal["id"] == goal_id:
                    if (goal["status"] == "removed") != restore:
                        return  # only Undo brings a removed goal back; Undo only applies to removed goals
                    if restore and len(self.active_goals(data)) >= MAX_GOALS:
                        raise GoalLimit()
                    if status == "edited":
                        new_text = clean_goal(text or "")
                        if not new_text:
                            return
                        if new_text == goal["text"]:
                            goal["status"] = "accepted"
                            return
                        goal.setdefault("original_text", goal["text"])
                        goal["text"] = new_text
                    goal["status"] = status
                    return
            raise NotFound(goal_id)

        return self.update(token, mutate, owner)

    @staticmethod
    def accepted_goals(data: dict) -> list[dict]:
        """What the owner took on: goals they wrote, accepted or changed. A suggestion nobody answered is not among them."""
        return [g for g in data.get("goals", []) if g["status"] in ACCEPTED]

    def confirm_goals(self, token: str, owner: str | None = None, accept_all: bool = False) -> dict:
        """The owner confirms the goals they accepted, and the project sentence with them. The first night runs only after this.
        accept_all (operator tool, tests): every open suggestion is accepted first."""

        def mutate(data):
            if accept_all:
                for goal in data.get("goals", []):
                    if goal["status"] == "proposed":
                        goal["status"] = "accepted"
            accepted = self.accepted_goals(data)
            if not accepted:
                raise NoGoalsAccepted()
            pitch = " ".join((data.get("pitch_line") or "").split())
            if pitch:
                problems = lint_pitch(pitch, project_names(data))
                if problems:
                    raise PitchInvalid(problems)
            data["confirmed_goals"] = [{"id": g["id"], "text": g["text"], "origin": g["origin"], "reason": g["reason"]} for g in accepted]
            data["confirmed_at"] = now()
            if pitch:
                data["pitch_line"], data["pitch_confirmed_at"] = pitch, now()

        return self.update(token, mutate, owner)

    def set_pitch(self, token: str, text: str, owner: str | None = None) -> dict:
        """The owner writes or changes the project sentence. No model call. An empty text removes it (the writers fall back to the old rule)."""
        text = " ".join((text or "").split())

        def mutate(data):
            if text:
                problems = lint_pitch(text, project_names(data))
                if problems:
                    raise PitchInvalid(problems)
            data["pitch_line"] = text
            # After the first confirmation the owner's own words count at once; before it, "Confirm" confirms them with the goals.
            data["pitch_confirmed_at"] = now() if text and data.get("confirmed_goals") is not None else None

        return self.update(token, mutate, owner)

"""Project storage. A JSON file per project for now; the same interface moves to Firestore.

Writes are field patches on a fresh read, never a blind overwrite of someone else's change.
"""

import json
import os
import secrets
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

MAX_GOALS = 20
MAX_GOAL_CHARS = 200
_TOKEN_RE_LEN = 22  # token_urlsafe(16) -> 128 bits


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_goal(text: str) -> str:
    return " ".join((text or "").split())[:MAX_GOAL_CHARS]


class NotFound(Exception):
    pass


class GoalLimit(Exception):
    pass


class ProjectStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, token: str) -> Path:
        if len(token) != _TOKEN_RE_LEN or not all(c.isalnum() or c in "-_" for c in token):
            raise NotFound(token)
        return self.root / f"{token}.json"

    def _write(self, path: Path, data: dict) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.root, suffix=".tmp")
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def create(self, data: dict) -> str:
        token = secrets.token_urlsafe(16)
        data = {**data, "id": token, "created_at": now()}
        with self._lock:
            self._write(self._path(token), data)
        return token

    def get(self, token: str) -> dict:
        path = self._path(token)
        if not path.exists():
            raise NotFound(token)
        return json.loads(path.read_text())

    def update(self, token: str, mutate) -> dict:
        """Read-modify-write under a lock. `mutate(data)` changes data in place."""
        with self._lock:
            data = self.get(token)
            mutate(data)
            data["updated_at"] = now()
            self._write(self._path(token), data)
            return data

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

    def add_goal(self, token: str, text: str) -> dict:
        text = clean_goal(text)

        def mutate(data):
            if not text:
                return
            if len(self.active_goals(data)) >= MAX_GOALS:
                raise GoalLimit()
            data.setdefault("goals", []).append(self.new_goal(text, "user"))

        return self.update(token, mutate)

    def set_goal_status(self, token: str, goal_id: str, status: str, text: str | None = None) -> dict:
        def mutate(data):
            for goal in data.get("goals", []):
                if goal["id"] == goal_id:
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

        return self.update(token, mutate)

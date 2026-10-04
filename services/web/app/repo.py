"""Storage interface and the file implementation (local development, tests, self-hosting without Firestore).

Both implementations (this one and `repo_firestore.py`) pass the same contract tests. The public mode never
touches this layer: it keeps no data on the server.

Writes that depend on a fresh read go through `mutate` callbacks, never a blind overwrite.
"""

import contextlib
import fcntl
import json
import os
import re
import secrets
import tempfile
import threading
import time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def now_precise() -> str:
    """Microsecond timestamp for things that are sorted newest first (runs, ledger lines): two in one second must keep their order."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def now_dt() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class NotFound(Exception):
    pass


Mutate = Callable[[dict], None]


class Repo(ABC):
    # --- projects (a project is a workspace) -------------------------------------------------
    @abstractmethod
    def create_project(self, owner: str, data: dict) -> str: ...

    @abstractmethod
    def get_project(self, pid: str) -> dict: ...

    @abstractmethod
    def update_project(self, pid: str, mutate: Mutate) -> dict: ...

    @abstractmethod
    def list_projects(self, owner: str | None = None) -> list[dict]: ...

    @abstractmethod
    def count_projects_since(self, owner: str, since_iso: str) -> int: ...

    # --- runs --------------------------------------------------------------------------------
    @abstractmethod
    def create_run(self, run: dict) -> None: ...

    @abstractmethod
    def get_run(self, run_id: str) -> dict: ...

    @abstractmethod
    def mutate_run(self, run_id: str, mutate: Mutate) -> dict: ...

    @abstractmethod
    def list_runs(self, project_id: str, limit: int = 10) -> list[dict]: ...

    @abstractmethod
    def save_checkpoint(self, run_id: str, data: dict) -> None: ...

    @abstractmethod
    def load_checkpoint(self, run_id: str) -> dict | None: ...

    # --- the lock against starting the same run twice ----------------------------------------
    @abstractmethod
    def acquire_lock(self, project_id: str, run_id: str, ttl_s: float) -> dict:
        """-> {acquired, holder, takeover, previous}. A lock whose heartbeat is older than ttl_s is stale."""

    @abstractmethod
    def heartbeat(self, project_id: str, run_id: str) -> bool:
        """False when the lock is no longer ours."""

    @abstractmethod
    def release_lock(self, project_id: str, run_id: str) -> None: ...

    # --- feedback ----------------------------------------------------------------------------
    @abstractmethod
    def add_feedback(self, project_id: str, fb: dict) -> str: ...

    @abstractmethod
    def list_feedback(self, project_id: str, unconsumed_only: bool = False) -> list[dict]: ...

    @abstractmethod
    def mark_feedback(self, project_id: str, ids: list[str], run_id: str) -> None: ...

    # --- ledger ------------------------------------------------------------------------------
    @abstractmethod
    def append_ledger(self, line: dict) -> None: ...

    @abstractmethod
    def ledger(self, project_id: str | None = None, limit: int = 100) -> list[dict]: ...

    @abstractmethod
    def month_spend(self, project_id: str | None, month: str) -> float: ...

    # --- allowlist and settings --------------------------------------------------------------
    @abstractmethod
    def allow_get(self, email: str) -> dict | None: ...

    @abstractmethod
    def allow_add(self, email: str, by: str) -> None: ...

    @abstractmethod
    def allow_remove(self, email: str) -> None: ...

    @abstractmethod
    def allow_list(self) -> list[dict]: ...

    @abstractmethod
    def get_settings(self) -> dict: ...

    @abstractmethod
    def set_settings(self, patch: dict) -> dict: ...

    # --- public-mode concurrency slots (a counter, no user data) ------------------------------
    @abstractmethod
    def acquire_slot(self, max_slots: int, ttl_s: float) -> str | None: ...

    @abstractmethod
    def release_slot(self, token: str) -> None: ...


def new_id(prefix: str = "") -> str:
    return prefix + secrets.token_urlsafe(16)


def check_id(value: str) -> str:
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise NotFound(str(value)[:40])
    return value


class FileRepo(Repo):
    """JSON files under one root. Safe across processes: every write holds an flock on `<root>/.lock`."""

    def __init__(self, root: Path, ledger_file: Path | None = None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        for sub in ("projects", "runs", "checkpoints", "locks", "feedback", "slots"):
            (self.root / sub).mkdir(exist_ok=True)
        self.ledger_file = Path(ledger_file) if ledger_file else self.root / "ledger.jsonl"
        self.ledger_file.parent.mkdir(parents=True, exist_ok=True)
        self._tlock = threading.RLock()
        self._lockfile = self.root / ".lock"

    # --- plumbing ----------------------------------------------------------------------------
    @contextlib.contextmanager
    def _locked(self):
        with self._tlock:
            fd = os.open(self._lockfile, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def _read(self, path: Path) -> dict | None:
        try:
            return json.loads(path.read_text())
        except FileNotFoundError:
            return None

    def _write(self, path: Path, data) -> None:
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def _doc(self, sub: str, doc_id: str) -> Path:
        return self.root / sub / f"{check_id(doc_id)}.json"

    # --- projects ----------------------------------------------------------------------------
    def create_project(self, owner: str, data: dict) -> str:
        pid = secrets.token_urlsafe(16)
        doc = {**data, "id": pid, "owner": owner, "created_at": now()}
        with self._locked():
            self._write(self._doc("projects", pid), doc)
        return pid

    def get_project(self, pid: str) -> dict:
        doc = self._read(self._doc("projects", pid))
        if doc is None:
            raise NotFound(pid)
        return doc

    def update_project(self, pid: str, mutate: Mutate) -> dict:
        with self._locked():
            doc = self.get_project(pid)
            mutate(doc)
            doc["updated_at"] = now()
            self._write(self._doc("projects", pid), doc)
            return doc

    def list_projects(self, owner: str | None = None) -> list[dict]:
        out = []
        for path in sorted((self.root / "projects").glob("*.json")):
            doc = self._read(path)
            if doc and (owner is None or doc.get("owner") == owner):
                out.append(doc)
        out.sort(key=lambda d: d.get("created_at", ""), reverse=True)
        return out

    def count_projects_since(self, owner: str, since_iso: str) -> int:
        return sum(1 for p in self.list_projects(owner) if p.get("created_at", "") >= since_iso)

    # --- runs --------------------------------------------------------------------------------
    def create_run(self, run: dict) -> None:
        with self._locked():
            self._write(self._doc("runs", run["run_id"]), run)

    def get_run(self, run_id: str) -> dict:
        doc = self._read(self._doc("runs", run_id))
        if doc is None:
            raise NotFound(run_id)
        return doc

    def mutate_run(self, run_id: str, mutate: Mutate) -> dict:
        with self._locked():
            doc = self.get_run(run_id)
            mutate(doc)
            doc["updated_at"] = now()
            self._write(self._doc("runs", run_id), doc)
            return doc

    def list_runs(self, project_id: str, limit: int = 10) -> list[dict]:
        runs = []
        for path in (self.root / "runs").glob("*.json"):
            doc = self._read(path)
            if doc and doc.get("project_id") == project_id:
                runs.append(doc)
        runs.sort(key=lambda d: d.get("started_at", ""), reverse=True)
        return runs[:limit]

    def save_checkpoint(self, run_id: str, data: dict) -> None:
        with self._locked():
            self._write(self._doc("checkpoints", run_id), data)

    def load_checkpoint(self, run_id: str) -> dict | None:
        return self._read(self._doc("checkpoints", run_id))

    # --- lock --------------------------------------------------------------------------------
    def acquire_lock(self, project_id: str, run_id: str, ttl_s: float) -> dict:
        path = self._doc("locks", project_id)
        with self._locked():
            cur = self._read(path)
            fresh = {"run_id": run_id, "heartbeat_at": now(), "pid": os.getpid()}
            if cur is None:
                self._write(path, fresh)
                return {"acquired": True, "holder": run_id, "takeover": False, "previous": None}
            beat = parse_ts(cur.get("heartbeat_at"))
            if beat is None or (now_dt() - beat).total_seconds() > ttl_s:
                self._write(path, fresh)
                return {"acquired": True, "holder": run_id, "takeover": True, "previous": cur.get("run_id")}
            return {"acquired": False, "holder": cur.get("run_id"), "takeover": False, "previous": None}

    def heartbeat(self, project_id: str, run_id: str) -> bool:
        path = self._doc("locks", project_id)
        with self._locked():
            cur = self._read(path)
            if not cur or cur.get("run_id") != run_id:
                return False
            cur["heartbeat_at"] = now()
            self._write(path, cur)
            return True

    def release_lock(self, project_id: str, run_id: str) -> None:
        path = self._doc("locks", project_id)
        with self._locked():
            cur = self._read(path)
            if cur and cur.get("run_id") == run_id:
                path.unlink(missing_ok=True)

    # --- feedback ----------------------------------------------------------------------------
    def _fb_path(self, project_id: str) -> Path:
        return self.root / "feedback" / f"{check_id(project_id)}.json"

    def add_feedback(self, project_id: str, fb: dict) -> str:
        fid = "f_" + secrets.token_hex(6)
        with self._locked():
            items = self._read(self._fb_path(project_id)) or []
            items.append({**fb, "id": fid, "created_at": now(), "consumed_by": None})
            self._write(self._fb_path(project_id), items)
        return fid

    def list_feedback(self, project_id: str, unconsumed_only: bool = False) -> list[dict]:
        items = self._read(self._fb_path(project_id)) or []
        if unconsumed_only:
            items = [i for i in items if not i.get("consumed_by")]
        return items

    def mark_feedback(self, project_id: str, ids: list[str], run_id: str) -> None:
        with self._locked():
            items = self._read(self._fb_path(project_id)) or []
            for item in items:
                if item["id"] in ids:
                    item["consumed_by"] = run_id
            self._write(self._fb_path(project_id), items)

    # --- ledger ------------------------------------------------------------------------------
    def append_ledger(self, line: dict) -> None:
        data = (json.dumps(line, ensure_ascii=False) + "\n").encode()
        with self._locked():
            fd = os.open(self.ledger_file, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)

    def _all_ledger(self) -> list[dict]:
        try:
            raw = self.ledger_file.read_text()
        except FileNotFoundError:
            return []
        out = []
        for line in raw.splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def ledger(self, project_id: str | None = None, limit: int = 100) -> list[dict]:
        lines = [l for l in self._all_ledger() if project_id is None or l.get("project_id") == project_id]
        lines.sort(key=lambda l: l.get("started_at", ""), reverse=True)  # newest first, as in Firestore
        return lines[:limit]

    def month_spend(self, project_id: str | None, month: str) -> float:
        total = 0.0
        for line in self._all_ledger():
            if project_id is not None and line.get("project_id") != project_id:
                continue
            if str(line.get("started_at", ""))[:7] != month:
                continue
            total += float(line.get("cost_eur") or 0.0)
        return total

    # --- allowlist and settings --------------------------------------------------------------
    def _allow(self) -> dict:
        return self._read(self.root / "allowlist.json") or {}

    def allow_get(self, email: str) -> dict | None:
        return self._allow().get(email.strip().lower())

    def allow_add(self, email: str, by: str) -> None:
        with self._locked():
            data = self._allow()
            data[email.strip().lower()] = {"email": email.strip().lower(), "added_by": by, "added_at": now()}
            self._write(self.root / "allowlist.json", data)

    def allow_remove(self, email: str) -> None:
        with self._locked():
            data = self._allow()
            data.pop(email.strip().lower(), None)
            self._write(self.root / "allowlist.json", data)

    def allow_list(self) -> list[dict]:
        return sorted(self._allow().values(), key=lambda d: d["email"])

    def get_settings(self) -> dict:
        return self._read(self.root / "settings.json") or {}

    def set_settings(self, patch: dict) -> dict:
        with self._locked():
            data = self.get_settings()
            data.update(patch)
            self._write(self.root / "settings.json", data)
            return data

    # --- public slots ------------------------------------------------------------------------
    def acquire_slot(self, max_slots: int, ttl_s: float) -> str | None:
        with self._locked():
            live = 0
            for path in (self.root / "slots").glob("*.json"):
                doc = self._read(path)
                if not doc or time.time() - doc["t"] > ttl_s:
                    path.unlink(missing_ok=True)
                else:
                    live += 1
            if live >= max_slots:
                return None
            token = secrets.token_urlsafe(12)
            self._write(self._doc("slots", token), {"t": time.time()})
            return token

    def release_slot(self, token: str) -> None:
        with self._locked():
            with contextlib.suppress(NotFound):
                self._doc("slots", token).unlink(missing_ok=True)


def open_repo() -> Repo:
    """The repo the configured backend asks for."""
    from . import config

    if config.BACKEND == "firestore":
        from .repo_firestore import FirestoreRepo

        return FirestoreRepo()
    return FileRepo(config.DATA_DIR, config.LEDGER_FILE)

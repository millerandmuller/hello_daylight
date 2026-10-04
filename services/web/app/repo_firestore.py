"""Firestore (Native) implementation of the repo. Same contract as FileRepo, same contract tests.

Only the server talks to Firestore (Admin credentials of the Cloud Run service account). The security rules in
`deploy/firestore.rules` deny every direct client access. Queries use equality filters only, so no composite index
is needed; sorting happens in the server.
"""

import secrets
import time

from google.cloud import firestore

from .repo import NotFound, Repo, check_id, now, now_dt, parse_ts


class FirestoreRepo(Repo):
    def __init__(self, client: "firestore.Client | None" = None, project: str | None = None):
        self.db = client or firestore.Client(project=project)

    # --- helpers -----------------------------------------------------------------------------
    def _ref(self, col: str, doc_id: str):
        return self.db.collection(col).document(check_id(doc_id))

    def _mutate(self, col: str, doc_id: str, mutate) -> dict:
        ref = self._ref(col, doc_id)

        @firestore.transactional
        def txn(transaction):
            snap = ref.get(transaction=transaction)
            if not snap.exists:
                raise NotFound(doc_id)
            doc = snap.to_dict()
            mutate(doc)
            doc["updated_at"] = now()
            transaction.set(ref, doc)
            return doc

        return txn(self.db.transaction())

    # --- projects ----------------------------------------------------------------------------
    def create_project(self, owner: str, data: dict) -> str:
        pid = secrets.token_urlsafe(16)
        self._ref("projects", pid).set({**data, "id": pid, "owner": owner, "created_at": now()})
        return pid

    def get_project(self, pid: str) -> dict:
        snap = self._ref("projects", pid).get()
        if not snap.exists:
            raise NotFound(pid)
        return snap.to_dict()

    def update_project(self, pid: str, mutate) -> dict:
        return self._mutate("projects", pid, mutate)

    def list_projects(self, owner: str | None = None) -> list[dict]:
        q = self.db.collection("projects")
        if owner is not None:
            q = q.where(filter=firestore.FieldFilter("owner", "==", owner))
        out = [s.to_dict() for s in q.stream()]
        out.sort(key=lambda d: d.get("created_at", ""), reverse=True)
        return out

    def count_projects_since(self, owner: str, since_iso: str) -> int:
        return sum(1 for p in self.list_projects(owner) if p.get("created_at", "") >= since_iso)

    # --- runs --------------------------------------------------------------------------------
    def create_run(self, run: dict) -> None:
        self._ref("runs", run["run_id"]).set(run)

    def get_run(self, run_id: str) -> dict:
        snap = self._ref("runs", run_id).get()
        if not snap.exists:
            raise NotFound(run_id)
        return snap.to_dict()

    def mutate_run(self, run_id: str, mutate) -> dict:
        return self._mutate("runs", run_id, mutate)

    def list_runs(self, project_id: str, limit: int = 10) -> list[dict]:
        q = self.db.collection("runs").where(filter=firestore.FieldFilter("project_id", "==", project_id))
        runs = [s.to_dict() for s in q.stream()]
        runs.sort(key=lambda d: d.get("started_at", ""), reverse=True)
        return runs[:limit]

    def save_checkpoint(self, run_id: str, data: dict) -> None:
        self._ref("checkpoints", run_id).set(data)

    def load_checkpoint(self, run_id: str) -> dict | None:
        snap = self._ref("checkpoints", run_id).get()
        return snap.to_dict() if snap.exists else None

    # --- lock --------------------------------------------------------------------------------
    def acquire_lock(self, project_id: str, run_id: str, ttl_s: float) -> dict:
        ref = self._ref("locks", project_id)

        @firestore.transactional
        def txn(transaction):
            snap = ref.get(transaction=transaction)
            fresh = {"run_id": run_id, "heartbeat_at": now()}
            if not snap.exists:
                transaction.set(ref, fresh)
                return {"acquired": True, "holder": run_id, "takeover": False, "previous": None}
            cur = snap.to_dict()
            beat = parse_ts(cur.get("heartbeat_at"))
            if beat is None or (now_dt() - beat).total_seconds() > ttl_s:
                transaction.set(ref, fresh)
                return {"acquired": True, "holder": run_id, "takeover": True, "previous": cur.get("run_id")}
            return {"acquired": False, "holder": cur.get("run_id"), "takeover": False, "previous": None}

        return txn(self.db.transaction())

    def heartbeat(self, project_id: str, run_id: str) -> bool:
        ref = self._ref("locks", project_id)

        @firestore.transactional
        def txn(transaction):
            snap = ref.get(transaction=transaction)
            if not snap.exists or snap.to_dict().get("run_id") != run_id:
                return False
            transaction.update(ref, {"heartbeat_at": now()})
            return True

        return txn(self.db.transaction())

    def release_lock(self, project_id: str, run_id: str) -> None:
        ref = self._ref("locks", project_id)

        @firestore.transactional
        def txn(transaction):
            snap = ref.get(transaction=transaction)
            if snap.exists and snap.to_dict().get("run_id") == run_id:
                transaction.delete(ref)

        txn(self.db.transaction())

    # --- feedback ----------------------------------------------------------------------------
    def add_feedback(self, project_id: str, fb: dict) -> str:
        fid = "f_" + secrets.token_hex(6)
        self.db.collection("feedback").document(fid).set(
            {**fb, "id": fid, "project_id": project_id, "created_at": now(), "consumed_by": None}
        )
        return fid

    def list_feedback(self, project_id: str, unconsumed_only: bool = False) -> list[dict]:
        q = self.db.collection("feedback").where(filter=firestore.FieldFilter("project_id", "==", project_id))
        items = [s.to_dict() for s in q.stream()]
        items.sort(key=lambda d: d.get("created_at", ""))
        if unconsumed_only:
            items = [i for i in items if not i.get("consumed_by")]
        return items

    def mark_feedback(self, project_id: str, ids: list[str], run_id: str) -> None:
        for fid in ids:
            self.db.collection("feedback").document(fid).update({"consumed_by": run_id})

    # --- ledger ------------------------------------------------------------------------------
    def append_ledger(self, line: dict) -> None:
        month = str(line.get("started_at", ""))[:7]
        self.db.collection("ledger").document().set({**line, "month": month})

    def ledger(self, project_id: str | None = None, limit: int = 100) -> list[dict]:
        q = self.db.collection("ledger")
        if project_id is not None:
            q = q.where(filter=firestore.FieldFilter("project_id", "==", project_id))
        lines = [s.to_dict() for s in q.stream()]
        lines.sort(key=lambda d: d.get("started_at", ""), reverse=True)
        return lines[:limit]

    def month_spend(self, project_id: str | None, month: str) -> float:
        q = self.db.collection("ledger").where(filter=firestore.FieldFilter("month", "==", month))
        if project_id is not None:
            q = q.where(filter=firestore.FieldFilter("project_id", "==", project_id))
        return sum(float(s.to_dict().get("cost_eur") or 0.0) for s in q.stream())

    # --- allowlist and settings --------------------------------------------------------------
    @staticmethod
    def _email_id(email: str) -> str:
        return email.strip().lower().replace("/", "_")

    def allow_get(self, email: str) -> dict | None:
        snap = self.db.collection("allowlist").document(self._email_id(email)).get()
        return snap.to_dict() if snap.exists else None

    def allow_add(self, email: str, by: str) -> None:
        e = email.strip().lower()
        self.db.collection("allowlist").document(self._email_id(e)).set({"email": e, "added_by": by, "added_at": now()})

    def allow_remove(self, email: str) -> None:
        self.db.collection("allowlist").document(self._email_id(email)).delete()

    def allow_list(self) -> list[dict]:
        return sorted((s.to_dict() for s in self.db.collection("allowlist").stream()), key=lambda d: d["email"])

    def get_settings(self) -> dict:
        snap = self.db.collection("settings").document("global").get()
        return snap.to_dict() if snap.exists else {}

    def set_settings(self, patch: dict) -> dict:
        ref = self.db.collection("settings").document("global")
        ref.set(patch, merge=True)
        return ref.get().to_dict() or {}

    # --- public slots ------------------------------------------------------------------------
    def acquire_slot(self, max_slots: int, ttl_s: float) -> str | None:
        col = self.db.collection("slots")
        token = secrets.token_urlsafe(12)
        ref = col.document(token)

        @firestore.transactional
        def txn(transaction):
            live = 0
            for snap in col.stream(transaction=transaction):
                if time.time() - float(snap.to_dict().get("t", 0)) > ttl_s:
                    transaction.delete(snap.reference)
                else:
                    live += 1
            if live >= max_slots:
                return None
            transaction.set(ref, {"t": time.time()})
            return token

        return txn(self.db.transaction())

    def release_slot(self, token: str) -> None:
        self._ref("slots", token).delete()

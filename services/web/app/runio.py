"""Where a run keeps its state: the repo (private mode) or nowhere (public mode)."""

import asyncio

from .engine import RunIO
from .repo import Repo


class RepoRunIO(RunIO):
    persistent = True

    def __init__(self, repo: Repo, project_id: str, run_id: str, emit_cb=None):
        self.repo, self.project_id, self.run_id = repo, project_id, run_id
        self._emit = emit_cb

    async def load(self) -> dict | None:
        return await asyncio.to_thread(self.repo.load_checkpoint, self.run_id)

    async def save(self, state: dict) -> None:
        await asyncio.to_thread(self.repo.save_checkpoint, self.run_id, state)

    async def view(self, patch: dict) -> None:
        await asyncio.to_thread(self.repo.patch_run, self.run_id, patch)

    async def beat(self) -> tuple[bool, bool]:
        ours = await asyncio.to_thread(self.repo.heartbeat, self.project_id, self.run_id)
        run = await asyncio.to_thread(self.repo.get_run, self.run_id)
        return ours, bool(run.get("cancel_requested"))

    async def finish(self, run_patch: dict, ledger_line: dict, project_patch) -> None:
        def work():
            self.repo.patch_run(self.run_id, run_patch)
            self.repo.append_ledger(ledger_line)
            if project_patch is not None:
                self.repo.update_project(self.project_id, project_patch)
            self.repo.release_lock(self.project_id, self.run_id)

        await asyncio.to_thread(work)

    def emit(self, event: dict) -> None:
        if self._emit:
            self._emit(event)


class MemoryRunIO(RunIO):
    """Public mode: nothing is stored. Events go to the browser; the ledger line carries cost, never content or key."""

    persistent = False

    def __init__(self, emit_cb, ledger_repo: Repo | None = None):
        self._emit = emit_cb
        self._ledger_repo = ledger_repo
        self.cancelled = False

    async def load(self) -> dict | None:
        return None

    async def save(self, state: dict) -> None:
        return None

    async def view(self, patch: dict) -> None:
        return None

    async def beat(self) -> tuple[bool, bool]:
        return True, self.cancelled

    async def finish(self, run_patch: dict, ledger_line: dict, project_patch) -> None:
        if self._ledger_repo is not None:
            # accounting only: no project words, no names the model chose, never a key
            line = {k: v for k, v in ledger_line.items() if k not in ("notes", "replaced_agents", "missing_tasks", "project_id")}
            line["replaced_agents"] = len(ledger_line.get("replaced_agents", []))
            line["missing_tasks"] = len(ledger_line.get("missing_tasks", []))
            line["project_id"] = None
            await asyncio.to_thread(self._ledger_repo.append_ledger, line)

    def emit(self, event: dict) -> None:
        self._emit(event)

"""Starts a run on click. The web service never runs a private run itself: Cloud Run throttles the CPU of a
service between requests. Locally a subprocess does the job; in the cloud a Cloud Run Job execution does."""

import logging
import os
import subprocess
import sys

from . import config

log = logging.getLogger("daylight.launcher")


def launch_run(project_id: str, budget: float | None = None) -> str:
    args = ["--project", project_id, "--trigger", "manual", "--wait-stale"]
    if budget is not None:
        args += ["--budget", f"{budget:.4f}"]
    if config.LAUNCHER == "cloudrun":
        return _cloud_run(args)
    log_path = config.DATA_DIR / f"night-{project_id[:8]}.log"
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    out = open(log_path, "ab")
    subprocess.Popen([sys.executable, "-m", "app.night", *args], stdout=out, stderr=out, start_new_session=True, env=os.environ.copy(), cwd=str(config.REPO_ROOT / "services" / "web"))
    return "subprocess"


def _cloud_run(args: list[str]) -> str:
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    session = AuthorizedSession(creds)
    body = {"overrides": {"containerOverrides": [{"args": ["-m", "app.night", *args]}]}}
    resp = session.post(f"https://run.googleapis.com/v2/{config.CLOUD_RUN_JOB}:run", json=body, timeout=20)
    if resp.status_code >= 300:
        log.error("job start failed: %s", resp.status_code)
        raise RuntimeError("could not start the run")
    return "cloudrun"

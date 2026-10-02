import os
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[3]

# Local development reads the key from the repo-root .env (gitignored).
# On Cloud Run the same variables come from Secret Manager.
load_dotenv(REPO_ROOT / ".env")

MODEL = os.getenv("DAYLIGHT_INTAKE_MODEL", "gemini-3.7-flash")
DATA_DIR = Path(os.getenv("DAYLIGHT_DATA_DIR", REPO_ROOT / ".data"))

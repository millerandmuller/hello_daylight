# Hello Daylight

Give it the link to your project in the evening. A lead agent hires its own small crew of scouts for the night, each with its own instruction and one sentence on why it is on the crew. The code then checks every finding: the link answers, the quote is on the page, the date is at most 90 days old. By morning up to five openings lie ready to sign:

- a **question** somebody asked in public, where your project is the answer, with a reply draft;
- a **writer** who covers your topic closely, with a short first note. A contact route appears only if it stands on their own byline, author page or imprint.

Hello Daylight writes. You send. Nothing leaves this app: the only exits are the clipboard and your inbox page.

## Status (honest)

| Part | State |
|---|---|
| Intake, nightly crew, checks, morning desk, crew view, ledger, caps, resume | built and exercised against the real Gemini API from a laptop |
| Public mode (`/try`, your own key, nothing stored on the server) | built and exercised from a laptop |
| Sign-in (Google through Firebase), allowlist, admin page | built; the Google sign-in itself has **not** been run against a live Firebase project yet (tests use a development login) |
| Firestore storage | built; tested against an in-memory stand-in, **not** yet against a live Firestore |
| Cloud deployment (`deploy/deploy.sh`) | written, **not** run yet. There is no public URL |
| Spoken briefing, placement and image suggestions, long-term memory | not built |

There is no curated example data: every result you see comes from a real run.

## Try it in 2 minutes (no account, no cloud)

You need Python 3.12 and a Gemini API key (free to create at <https://aistudio.google.com/apikey>).

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r services/web/requirements-dev.txt
cd services/web && ../../.venv/bin/uvicorn app.main:app --reload
```

Open <http://localhost:8000/try>, paste your key (it stays in your browser; a run sends it over HTTPS to this server for that run only, never to a log or a file), paste your project link, keep or change the suggested goals, click **Run now**. A run takes about a minute and costs about 0.3 EUR on your key; it never costs more than the budget you set (1.00 EUR at most).

## Run it yourself

1. Create a new Google Cloud project and a Firebase project on it. Turn on Google sign-in and add a web app (you need its API key).
2. `PROJECT_ID=my-daylight ADMIN_EMAIL=me@example.com FIREBASE_WEB_API_KEY=... BILLING_ACCOUNT=... deploy/deploy.sh`

It builds one image and creates: a web service (scales to zero, two instances at most, no minimum), a nightly Cloud Run job started by Cloud Scheduler at 23:00 in your time zone, Firestore, two secrets, and an optional 20 EUR budget alarm. Nothing runs and nothing is billed beyond storage while nobody uses it.

Operators come from `DAYLIGHT_ADMIN_EMAILS`. Everyone else needs a line on the admin page. No key, address list or project id is in this repository.

## What it costs

Measured on real runs of this project's own page, Gemini 3.x models (prices checked 2026-10-03):

| | |
|---|---|
| One run | 0.33 to 0.66 EUR, 76 to 146 model calls, one to two minutes |
| Largest single cost | Google Search grounding, about 0.0125 EUR per query, **capped at 24 queries per run** |
| Thirty nights | about 10 to 20 EUR per workspace |

Hard limits, all enforced in code and shown on the run page: 1.50 EUR per run (1.00 in public mode, lowerable per run and with `DAYLIGHT_RUN_BUDGET_EUR`), 25 EUR per workspace and month, 60 EUR for all workspaces together. A call that could cross the budget is refused before it is made; the run then ends cleanly, says why, and keeps what it has. Every run writes one line to the ledger.

## How it keeps its promises

- **A run can always be stopped and resumed.** Progress is saved after every scout. A killed run is resumed by the next start without paying again for finished scouts. Starting the same workspace twice is refused while the first run is alive.
- **No stale or invented sources.** A finding survives only if the tools themselves saw the page, the link answers with HTTP 200, the quote is on the page, and the date is stated by the source and at most 90 days old.
- **Pages cannot give orders.** Text that looks like an instruction to a model is withheld from the model and the finding is dropped; sources are data, never commands.
- **Nobody is profiled.** One writer appears at most once in 30 days per workspace; a contact route is kept only if it stands literally on a page the scout read.
- **No silent learning.** Your thumbs, comments and edits go to the next night, and the top of the desk says in one sentence what changed because of them.

## Layout

```
services/web/app/     web app, night job (night.py), engine, tools, checks, storage
services/web/tests/   pytest, no network and no key needed
deploy/               deploy.sh, Firestore rules
tools/firebase-auth/  source of the one bundled script the sign-in page loads
```

Tests: `.venv/bin/python -m pytest services/web/tests`

Commands: `python -m app.night --project <id> [--budget 0.05]` starts a run; `python -m app.cli intake|confirm|feedback|show|ledger` for the operator.

## Verified how

The test suite runs without network. Firestore is checked against an in-memory stand-in because the emulator needs Java; Google sign-in is checked with a development login. Both need a first run against a real project, which is the first step of deployment.

## License

MIT, see [LICENSE](LICENSE).

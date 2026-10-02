# Hello Daylight

Throw in the link to your project in the evening. A planner agent puts together its own crew of sub-agents for the night — each with its own instructions, visible while it works, replacing the ones that come back with weak results. Wake up to five openings ready to sign: real questions where your project is the answer, and writers already covering your topic closely enough to have a natural interest.

You sign, you send. Nothing leaves this app on its own.

## Status

Early build. What's real right now:

- [x] Project intake: paste a URL, get a project card and suggested goals, each with a reason. Keep, change or remove any of them and add your own. Works locally; not deployed yet.
- [ ] Nightly agent crew (visible task stream, hire/fire on weak results)
- [ ] Morning desk (question + resonance cards, sign/edit/thumbs)
- [ ] Spoken morning briefing

Nothing is deployed publicly yet. This README will say exactly what's real and what's curated example data once there is something to try.

## What it explicitly does not do

- Never delivers to anyone but the project owner. No DMs, no cold emails to strangers, no platform write access.
- Never posts on your behalf anywhere.
- Never builds a list of people. A writer shows up on a card only with the contact route their own byline already gives.

## Running locally

Python 3.11.

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r services/web/requirements-dev.txt
cp .env.example .env   # add your Gemini API key
.venv/bin/uvicorn app.main:app --app-dir services/web --reload
```

Open http://localhost:8000 and paste a project link. `GET /status` returns a health check.

Tests: `.venv/bin/python -m pytest services/web/tests`

Projects are stored as JSON files under `.data/` for now (gitignored).

## License

MIT — see [LICENSE](LICENSE).

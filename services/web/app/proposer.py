"""The planner's first job: read the project and suggest goals, each with a reason."""

import os
from datetime import date

from pydantic import BaseModel, Field

from . import config
from .fetcher import PageSnapshot

TIMEOUT_MS = 20_000


class GoalSuggestion(BaseModel):
    text: str = Field(description="Short goal in plain words, 2-6 words, e.g. 'First users'.")
    reason: str = Field(description="One sentence. Starts with what the page shows, e.g. 'The page is three weeks old and names no prices.'")


class ProjectCard(BaseModel):
    name: str
    one_liner: str = Field(description="What the project does, one plain sentence.")
    audience: str = Field(description="Who it is probably for, one sentence, stated as a guess.")
    observations: list[str] = Field(description="2-4 short facts read directly off the page.")


class Proposal(BaseModel):
    project: ProjectCard
    goals: list[GoalSuggestion]


class ProposalError(Exception):
    pass


PROMPT = """You read an indie builder's project and suggest what they should look for first.

Today is {today}.

Rules:
- Use only what the material below says. Never invent users, numbers, dates, prices or features.
- If something is unclear, say it is a guess ("probably", "seems").
- Voice: plain, short, second person where you address the owner, no exclamation marks, no marketing adjectives.
- Suggest {n_min}-{n_max} goals. Each goal is short (2-6 words) and has one reason that points at
  something visible in the material (age of the page, missing prices, a waitlist, a changelog, a GitHub repo, a newsletter archive ...).
- A goal is always about people outside the project that a search crew can find in public places
  (forum threads, issues, articles, newsletters, podcasts): first users, paying customers, press or podcast coverage,
  feedback from a specific group, beta testers, contributors. Never an internal task (triaging issues, writing docs,
  redesigning the page, raising prices). Name the group as specifically as the material allows.
- The owner already has these goals of their own. Do not repeat or rephrase them, suggest only additional ones:
{user_goals}
- Write everything in English.

Material:
{material}
"""


def _material(page: PageSnapshot | None, description: str | None) -> str:
    parts = []
    if page:
        parts += [
            f"URL: {page.final_url}",
            f"Title: {page.title}",
            f"Meta description: {page.description}",
            f"Headings: {' | '.join(page.headings)}",
            f"Dates on the page: {', '.join(page.dates_found) or 'none found'}",
            f"Mentions pricing: {'yes' if page.mentions_pricing else 'no'}",
            f"Last-Modified header: {page.last_modified or 'not sent'}",
            f"Page text: {page.text}",
        ]
    if description:
        parts.append(f"Owner's own description: {description}")
    return "\n".join(parts)


def propose(page: PageSnapshot | None, description: str | None, user_goals: list[str]) -> Proposal:
    from google import genai
    from google.genai import types

    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise ProposalError("No Gemini API key configured.")
    n_min, n_max = (1, 3) if user_goals else (2, 4)
    prompt = PROMPT.format(
        today=date.today().isoformat(),
        n_min=n_min,
        n_max=n_max,
        user_goals="\n".join(f"  - {g}" for g in user_goals) or "  (none)",
        material=_material(page, description),
    )
    client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=TIMEOUT_MS))
    try:
        response = client.models.generate_content(
            model=config.MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=Proposal,
                temperature=0.4,
            ),
        )
    except Exception as exc:  # network, quota, timeout: all end in the same visible retry state
        raise ProposalError(f"Model call failed: {type(exc).__name__}") from exc
    proposal = response.parsed
    if not isinstance(proposal, Proposal):
        try:
            proposal = Proposal.model_validate_json(response.text or "")
        except Exception as exc:
            raise ProposalError("Model answer was not valid.") from exc
    proposal.goals = [g for g in proposal.goals if g.text.strip()][:n_max]
    return proposal

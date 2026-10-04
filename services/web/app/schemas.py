"""The shapes the agents answer in. Anything that does not fit is a bad answer, not a result."""

from typing import Literal

from pydantic import BaseModel, Field


class TaskSpec(BaseModel):
    role: str = Field(description="Short job title of this scout, e.g. 'Scout for questions on Hacker News'.")
    kind: Literal["question", "resonance"] = Field(
        description="'question': a public question the project answers. 'resonance': a writer who covers the topic closely."
    )
    instruction: str = Field(description="The complete, self-contained instruction for this scout: what to search, where, what a good find looks like.")
    tools: list[str] = Field(description="Which tools this scout may use, a subset of the tool names given.")
    rationale: str = Field(description="One sentence: why this scout is on the crew.")


class Plan(BaseModel):
    feedback_note: str = Field(description="One sentence naming what changed in this plan because of the owner's feedback. Empty string when there is no feedback.")
    tasks: list[TaskSpec]


class Finding(BaseModel):
    url: str = Field(description="Exactly a URL that a tool returned in this task.")
    why: str = Field(description="One sentence, only facts the page shows, why it fits the goals.")
    quote: str = Field(description="A short passage copied word for word from that page's snippet or text, at most 200 characters.")
    author_name: str | None = Field(default=None, description="Resonance only: the author's name as the page shows it.")
    contact_route: str | None = Field(default=None, description="Resonance only: an email address or profile link exactly as the byline, author page or imprint shows it. Never guess.")
    contact_source_url: str | None = Field(default=None, description="The page, read with read_page, where that contact route is shown.")


class ScoutOutput(BaseModel):
    findings: list[Finding] = Field(description="At most four findings; an empty list when nothing good was found.")
    nothing_found_because: str | None = Field(default=None, description="One sentence, when the list is empty.")


class Evaluation(BaseModel):
    score: int = Field(description="0 to 10: how useful this scout's verified results are for the owner's goals.")
    verdict: Literal["keep", "replace"]
    reason: str = Field(description="One plain sentence the owner will read.")
    new_instruction: str | None = Field(default=None, description="When replacing: a different angle (other words, other sources), at most 80 words.")


class Pick(BaseModel):
    item_id: str
    fit: str = Field(description="One sentence: why this one is among the best five for the goals.")


class Curation(BaseModel):
    picks: list[Pick]


class Draft(BaseModel):
    text: str = Field(description="The draft, plain text, paragraphs separated by a blank line.")


class Critique(BaseModel):
    ok: bool = Field(description="True only when the draft follows every rule and every claim is backed by the source.")
    problems: list[str] = Field(description="At most three concrete problems to fix. Empty when ok.")

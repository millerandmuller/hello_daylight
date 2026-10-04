"""Fixed texts and plain-code checks around a card. No model writes any of this.

- where the reply belongs (one fixed line per source),
- how old the find is and how many replies it already has,
- when two drafts of one night end the same way,
- the one fixed sentence that adds the project to a GitHub reply, only when the owner clicks for it.
"""

import re
from datetime import date

# Two drafts of one night whose last paragraphs share more than this share of their words read like a template.
CLOSING_SIMILARITY_MAX = 0.6

PUBLIC_COMMENT_LINE = "No public contact route found. The lowest-intrusion way is a public comment under the post:"
ROUTE_LINES = {
    "hn": "Reply in the thread:",
    "github": "Reply in the issue:",
}
OTHER_QUESTION_LINE = "Reply where it was posted:"
HAS_ROUTE_LINE = "Use the contact route shown above."

# A GitHub reply carries no project by default. This is the sentence the click adds: fixed, checked once, no model call.
LINK_SENTENCE = "If it is useful: I made {project}, which touches this topic. {url}"
LINK_ADDED_FLAG = "link_added"


def route_text(card: dict) -> tuple[str, str]:
    """(fixed words, link) for the line that says where this card belongs. The link is empty when the line has none.
    A writer's contact route is never produced here: it stays the one copied word for word from their own page."""
    if card.get("kind") == "resonance":
        if card.get("contact_route"):
            return HAS_ROUTE_LINE, ""
        return PUBLIC_COMMENT_LINE, card.get("url", "")
    return ROUTE_LINES.get(card.get("source", ""), OTHER_QUESTION_LINE), card.get("url", "")


def route_line(card: dict) -> str:
    words, url = route_text(card)
    return f"{words} {url}." if url else words


def age_days(day: str | None, today: date) -> int | None:
    if not day:
        return None
    try:
        return max(0, (today - date.fromisoformat(day[:10])).days)
    except ValueError:
        return None


def age_label(days: int | None, date_basis: str = "api") -> str:
    if days is None:
        return ""
    label = "today" if days == 0 else "1 day old" if days == 1 else f"{days} days old"
    if date_basis == "markup":
        label += " (date from page markup)"
    return label


def replies_label(replies: int | None) -> str:
    """Only a number the source gave. Nothing is made up for a source that has none."""
    if replies is None:
        return ""
    return f"{replies} {'reply' if replies == 1 else 'replies'}"


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9']+", text.lower()))


def closing_paragraph(text: str) -> str:
    paragraphs = [p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    return paragraphs[-1] if paragraphs else ""


def closing_similarity(a: str, b: str) -> float:
    """Share of words the two last paragraphs have in common (shared / all different words)."""
    wa, wb = _words(closing_paragraph(a)), _words(closing_paragraph(b))
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def same_closing_as(card_id: str, draft: str, others: dict[str, str]) -> str | None:
    """The id of the earlier card (lower number) whose closing this draft repeats, or None.
    Only the later card yields, so exactly one of two similar drafts is rewritten."""
    mine = int(card_id.lstrip("k") or 0)
    for other_id in sorted(others, key=lambda i: int(i.lstrip("k") or 0)):
        if int(other_id.lstrip("k") or 0) >= mine:
            continue
        if closing_similarity(draft, others[other_id]) > CLOSING_SIMILARITY_MAX:
            return other_id
    return None


def closing_hint(other_id: str) -> str:
    return f"same closing as card {other_id}"


def link_sentence(project: str, url: str) -> str:
    return LINK_SENTENCE.format(project=project, url=url)


def add_link(draft: str, sentence: str) -> str:
    return draft if sentence in draft else f"{draft.rstrip()}\n\n{sentence}"


def remove_link(draft: str, sentence: str) -> str:
    return draft.replace(f"\n\n{sentence}", "").replace(sentence, "").rstrip()


def strip_project(text: str, names: list[str], url: str) -> str:
    """For a reply that must not carry the project: drop every paragraph that names it or links it."""
    keep = []
    for para in [p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]:
        low = para.lower()
        if url and url.lower().rstrip("/") in low:
            continue
        if any(n and len(n) >= 3 and re.search(rf"\b{re.escape(n.lower())}\b", low) for n in names):
            continue
        keep.append(para)
    return "\n\n".join(keep)

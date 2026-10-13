"""'Sent' and 'Got a reply' in the inbox. Reversible, never fed to a plan, a scout or a writer, and never
a feedback entry (only the next run's header names what changed)."""

from app import cards as cardtext
from app.repo import now, now_precise

from .test_api_private import HX, OWNER, with_cards
from .test_engine import last_run, pid, run, world  # noqa: F401 - the full-run fixtures and helper


# --- the sentence, as a pure function ---------------------------------------------------------------
def test_mailbox_summary_is_empty_when_nothing_happened():
    assert cardtext.mailbox_summary(0, 0) == ""


def test_mailbox_summary_counts_sent_and_replies_with_the_right_plural():
    assert cardtext.mailbox_summary(1, 0) == "Since last night: 1 sent."
    assert cardtext.mailbox_summary(1, 1) == "Since last night: 1 sent, 1 reply came back."
    assert cardtext.mailbox_summary(2, 3) == "Since last night: 2 sent, 3 replies came back."


# --- the routes: reversible, signed-only, no feedback entry -----------------------------------------
def test_sent_and_replied_are_reversible_and_create_no_feedback(client, repo):
    token = with_cards(client, repo)
    client.post(f"/p/{token}/cards/k1/sign", headers=HX)

    client.post(f"/p/{token}/cards/k1/sent", headers=HX)
    card = repo.get_run("run-done-0001")["cards"][0]
    assert card["sent_at"] and not card.get("replied_at")

    client.post(f"/p/{token}/cards/k1/replied", data={"note": "  They wrote back,   thanks "}, headers=HX)
    card = repo.get_run("run-done-0001")["cards"][0]
    assert card["replied_at"] and card["reply_note"] == "They wrote back, thanks"

    client.post(f"/p/{token}/cards/k1/unreplied", headers=HX)
    card = repo.get_run("run-done-0001")["cards"][0]
    assert not card["replied_at"] and card["reply_note"] == "" and card["sent_at"]

    client.post(f"/p/{token}/cards/k1/unsent", headers=HX)
    card = repo.get_run("run-done-0001")["cards"][0]
    assert not card["sent_at"] and not card["replied_at"]

    assert repo.list_feedback(token) == []


def test_sent_does_nothing_on_an_unsigned_card(client, repo):
    token = with_cards(client, repo)
    client.post(f"/p/{token}/cards/k1/sent", headers=HX)
    assert repo.get_run("run-done-0001")["cards"][0].get("sent_at") is None


def test_replied_does_nothing_before_sent(client, repo):
    token = with_cards(client, repo)
    client.post(f"/p/{token}/cards/k1/sign", headers=HX)
    client.post(f"/p/{token}/cards/k1/replied", data={"note": "early"}, headers=HX)
    assert repo.get_run("run-done-0001")["cards"][0].get("replied_at") is None


def test_the_inbox_shows_sent_and_reply_state(client, repo):
    token = with_cards(client, repo)
    client.post(f"/p/{token}/cards/k1/sign", headers=HX)
    client.post(f"/p/{token}/cards/k1/sent", headers=HX)
    client.post(f"/p/{token}/cards/k1/replied", data={"note": "Said yes to a call."}, headers=HX)
    inbox = client.get(f"/p/{token}/inbox").text
    assert "Reply came back" in inbox and "Said yes to a call." in inbox


# --- the next run's header, and silence toward the model --------------------------------------------
def test_the_next_run_names_sent_and_replied_but_never_feeds_them_to_a_model(repo, pid, world):
    ledger1, _ = run(repo, pid, world)
    run1 = last_run(repo, pid)
    card_id = run1["cards"][0]["id"]
    secret = "REPLY_NOTE_7f3a_do_not_leak"

    def mark(doc):
        for c in doc["cards"]:
            if c["id"] == card_id:
                c["state"], c["sent_at"], c["replied_at"], c["reply_note"] = "signed", now_precise(), now_precise(), secret

    repo.mutate_run(run1["run_id"], mark)

    ledger2, gw2 = run(repo, pid, world)
    run2 = last_run(repo, pid)
    assert run2["mailbox_note"] == "Since last night: 1 sent, 1 reply came back."
    assert not any(secret in (instruction or "") or secret in (user_text or "") for _, _, instruction, user_text in gw2.instructions)
    assert repo.list_feedback(pid) == []


def test_a_first_ever_run_has_no_mailbox_note(repo, pid, world):
    run(repo, pid, world)
    assert last_run(repo, pid).get("mailbox_note") == ""

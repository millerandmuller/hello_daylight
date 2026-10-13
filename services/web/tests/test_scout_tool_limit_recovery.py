"""A scout that runs out of tool calls still answers with what it already found, instead of ending as a
missing part (ledger notes from real nights: 'did not finish (limit)')."""

from app.llm import CallFailed
from app.schemas import Finding, ScoutOutput

from .test_engine import last_run, pid, run, world  # noqa: F401 - the full-run fixtures and helper


def test_a_scout_that_hits_its_tool_limit_still_answers_with_what_it_has(repo, pid, world):
    def stubborn(gw):
        async def scout(tools, name):
            await tools["hn_search"]("first query")
            await tools["hn_search"]("second query")
            raise CallFailed("limit", "used all its model rounds")  # the model kept trying tools instead of answering

        gw.scout_script["scout_t1"] = scout
        gw.recover_script["scout_recover_t1"] = ScoutOutput(
            findings=[Finding(url="https://news.ycombinator.com/item?id=1000", why="A real question.", quote="how do you find the first ten users", author_name="person0")]
        )

    ledger, gw = run(repo, pid, world, gw_setup=stubborn)
    t1 = next(t for t in last_run(repo, pid)["plan_view"] if t["id"] == "t1")
    assert t1["status"] == "done"
    assert t1["found"] == 1
    assert all(m["task_id"] != "t1" for m in ledger["missing_tasks"])
    assert not any("did not finish (limit)" in n for n in last_run(repo, pid)["notes"])
    assert ("scout_recover", "scout_recover_t1") in gw.calls


def test_a_scout_that_hits_the_limit_with_nothing_found_still_ends_missing(repo, pid, world):
    def stubborn_empty(gw):
        async def scout(tools, name):
            raise CallFailed("limit", "used all its model rounds")  # no tool call ever ran: nothing to recover from

        gw.scout_script["scout_t1"] = scout

    ledger, gw = run(repo, pid, world, gw_setup=stubborn_empty)
    t1 = next(t for t in last_run(repo, pid)["plan_view"] if t["id"] == "t1")
    assert t1["status"] == "missing"
    assert any(m["task_id"] == "t1" for m in ledger["missing_tasks"])
    assert not any(s == "scout_recover" for s, _ in gw.calls)


def test_a_scout_that_times_out_is_not_sent_to_the_recovery_call(repo, pid, world):
    """Recovery is only for the tool-limit case (reason 'limit'): a timeout or provider error gets no second call."""
    ledger, gw = run(repo, pid, world, gw_setup=lambda g: g.fail_scouts.add("scout_t2"))
    assert ledger["status"] == "partial"
    assert [m["task_id"] for m in ledger["missing_tasks"]] == ["t2"]
    assert not any(s == "scout_recover" for s, _ in gw.calls)

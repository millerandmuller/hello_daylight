"""The model-call rules, through the real ADK runner with a scripted model: timeout, per-call retry, degrade, kill switch."""

import asyncio

import pytest
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from app import config
from app.llm import CallFailed, ModelGateway
from app.meter import RunMeter, RunStopped
from app.schemas import Critique


class FakeLlm(BaseLlm):
    model: str = "gemini-3.5-flash-lite"
    script: list = []
    seen: int = 0

    async def generate_content_async(self, llm_request, stream=False):
        self.seen += 1
        step = self.script.pop(0)
        kind = step[0]
        if kind == "raise":
            raise step[1]
        if kind == "hang":
            await asyncio.sleep(30)
        usage = types.GenerateContentResponseUsageMetadata(prompt_token_count=step[2][0], candidates_token_count=step[2][1])
        yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text=step[1])]), usage_metadata=usage)


GOOD = ('{"ok": true, "problems": []}', (1000, 100))
BAD = ("not json at all", (1000, 100))


def gateway(script, budget=1.0, monkeypatch=None, **contract_kw):
    contract = config.contract_for("private", budget)
    meter = RunMeter(contract)
    gw = ModelGateway("AIza-test-key-0123456789", meter)
    llm = FakeLlm(script=list(script))
    gw._llms["cheap"] = llm
    return gw, meter, llm


def call(gw, **kw):
    return asyncio.run(gw.run_agent(step="scout", tier="cheap", name="t", instruction="Check it.", user_text="go", output_schema=Critique, thinking=None, **kw))


def test_a_call_is_booked_with_its_real_token_cost():
    gw, meter, llm = gateway([("ok",) + GOOD])
    res = call(gw)
    assert res.parsed.ok is True
    price_in, price_out = config.price_for("gemini-3.5-flash-lite")
    assert meter.spent_eur == pytest.approx((1000 * price_in + 100 * price_out) / 1e6, rel=1e-6)
    assert meter.calls == 1 and meter.reserved_eur == 0


def test_the_kill_switch_refuses_the_call_before_it_is_made():
    gw, meter, llm = gateway([("ok",) + GOOD], budget=0.0)
    with pytest.raises(RunStopped) as err:
        call(gw)
    assert err.value.reason == "budget" and llm.seen == 0, "the model was never asked"


def test_the_kill_switch_fires_when_the_next_call_could_cross_the_budget():
    gw, meter, llm = gateway([("ok",) + GOOD, ("ok",) + GOOD], budget=0.0070)
    call(gw)
    with pytest.raises(RunStopped) as err:
        call(gw)
    assert err.value.reason == "budget" and llm.seen == 1


def test_a_timeout_is_retried_per_call_and_counted():
    gw, meter, llm = gateway([("raise", TimeoutError()), ("ok",) + GOOD])
    res = call(gw)
    assert res.parsed.ok and meter.retries == 1 and llm.seen == 2


def test_a_quota_error_is_retried_but_a_programming_error_is_not():
    class _ResourceExhaustedError(Exception):
        pass

    gw, meter, llm = gateway([("raise", _ResourceExhaustedError("429")), ("ok",) + GOOD])
    assert call(gw).parsed.ok and meter.retries == 1
    gw2, meter2, llm2 = gateway([("raise", KeyError("bug")), ("ok",) + GOOD])
    with pytest.raises(CallFailed) as err:
        call(gw2)
    assert err.value.reason == "provider" and llm2.seen == 1 and meter2.retries == 0


def test_retries_end_and_the_last_failure_is_reported():
    gw, meter, llm = gateway([("raise", TimeoutError())] * 4)
    with pytest.raises(CallFailed) as err:
        call(gw)
    assert err.value.reason == "timeout" and meter.retries == 2 and llm.seen == 3


def test_retries_count_against_the_retry_cap(monkeypatch):
    monkeypatch.setenv("DAYLIGHT_MAX_RETRIES", "1")
    gw, meter, llm = gateway([("raise", TimeoutError())] * 4)
    with pytest.raises(RunStopped) as err:
        call(gw)
    assert err.value.reason == "max_retries"


def test_a_call_that_never_answers_ends_at_the_deadline_and_gives_the_reservation_back():
    gw, meter, llm = gateway([("hang",)])
    with pytest.raises(CallFailed) as err:
        call(gw, deadline_s=0.3)
    assert err.value.reason == "deadline" and meter.reserved_eur == 0


def test_an_unusable_answer_is_tried_once_more_and_then_given_up():
    gw, meter, llm = gateway([("ok",) + BAD, ("ok",) + BAD, ("ok",) + GOOD])
    with pytest.raises(CallFailed) as err:
        call(gw)
    assert err.value.reason == "bad_output" and llm.seen == 2


def test_timeout_and_retry_live_in_one_http_options_object():
    meter = RunMeter(config.contract_for("private"))
    gw = ModelGateway("AIza-test-key-0123456789", meter)
    for tier in ("cheap", "mid", "strong"):
        opts = gw._llm(tier).api_client._api_client._http_options
        assert opts.timeout == config.CALL_TIMEOUT_MS[tier]
        assert opts.retry_options is not None and opts.retry_options.attempts == config.REQUEST_RETRY_ATTEMPTS


def test_the_key_is_never_part_of_what_the_gateway_prints():
    gw = ModelGateway("AIza-test-key-0123456789", RunMeter(config.contract_for("private")))
    assert "AIza" not in repr(gw.meter.summary()) and "AIza" not in str(gw.meter.state())


def test_meter_state_survives_a_checkpoint_roundtrip():
    gw, meter, llm = gateway([("ok",) + GOOD])
    call(gw)
    again = RunMeter(config.contract_for("private"), meter.state())
    assert again.spent_eur == pytest.approx(meter.spent_eur, abs=1e-5) and again.calls == meter.calls

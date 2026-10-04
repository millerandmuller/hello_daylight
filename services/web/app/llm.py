"""The only module that talks to a model. It enforces the model-call rules of the run contract:

  R1  every call has a timeout: `timeout` and `retry_options` live in ONE http_options object of the client
  R2  retries are per call (per node), scoped to errors worth retrying, and every retry counts against max_retries
  R3  a late or failed sub-agent degrades into a missing part that is named, it does not abort the run

and the budget kill switch: a call that would cross the budget is refused before it is made.
Provider, model ids and the key live in config / the caller, never here.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass, field

from google import genai
from google.adk.agents import LlmAgent
from google.adk.agents.run_config import RunConfig
from google.adk.models.google_llm import Gemini
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel, ValidationError

from . import config
from .logsafe import safe_exc, scrub
from .meter import RunMeter, RunStopped

log = logging.getLogger("daylight.llm")

RETRY_BACKOFF_S = (1.0, 3.0)
_STOP_CODES = {"BUDGET": "budget", "MAX_STEPS": "max_steps", "CANCELLED": "cancelled", "MAX_RETRIES": "max_retries"}


class CallFailed(Exception):
    """A call or sub-agent ended without a usable answer. `reason`: timeout, provider, quota, bad_output, deadline."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


@dataclass
class AgentResult:
    text: str = ""
    parsed: BaseModel | None = None
    grounding: list[dict] = field(default_factory=list)  # [{"uri","title"}] from search grounding
    search_queries: list[str] = field(default_factory=list)


class _Retryable(Exception):
    """ADK hands a model failure back as an event whose error_code is the exception's class name; this carries it on."""

    def __init__(self, reason: str, code: str):
        super().__init__(f"{reason}: {code}")
        self.reason = reason
        self.code = code


_RETRY_BY_NAME = {
    "TimeoutError": "timeout", "ReadTimeout": "timeout", "ConnectTimeout": "timeout", "TimeoutException": "timeout",
    "_ResourceExhaustedError": "quota", "ServerError": "provider", "ClientConnectorError": "provider",
    "ServerDisconnectedError": "provider", "ClientOSError": "provider", "ConnectError": "provider", "RemoteProtocolError": "provider",
}


def _retryable(exc: BaseException) -> str | None:
    """Reason string when the error is worth a retry, else None. Matches exact class names as ADK 2.8.0 does."""
    if isinstance(exc, _Retryable):
        return exc.reason
    name = type(exc).__name__
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)) or name in ("TimeoutError", "ReadTimeout", "ConnectTimeout"):
        return "timeout"
    if name == "_ResourceExhaustedError" or (isinstance(exc, genai_errors.ClientError) and getattr(exc, "code", 0) == 429):
        return "quota"
    if isinstance(exc, genai_errors.ServerError):
        return "provider"
    if name == "LlmCallsLimitExceededError":
        return None  # not worth paying again: the scout used all its rounds
    if name in ("ClientConnectorError", "ServerDisconnectedError", "ClientOSError", "ConnectError", "RemoteProtocolError"):
        return "provider"
    return None


def _request_chars(llm_request) -> int:
    total = 0
    cfg = getattr(llm_request, "config", None)
    si = getattr(cfg, "system_instruction", None)
    if si is not None:
        total += len(str(si)) if isinstance(si, str) else sum(len(getattr(p, "text", "") or "") for p in getattr(si, "parts", []) or [])
    for content in getattr(llm_request, "contents", None) or []:
        for part in getattr(content, "parts", None) or []:
            total += len(getattr(part, "text", "") or "")
            fr = getattr(part, "function_response", None)
            if fr is not None:
                total += len(str(getattr(fr, "response", "")))
            fc = getattr(part, "function_call", None)
            if fc is not None:
                total += len(str(getattr(fc, "args", "")))
    return total


class ModelGateway:
    """One per run. Holds the client (key in memory only) and the meter."""

    def __init__(self, api_key: str, meter: RunMeter):
        if not api_key:
            raise ValueError("no api key")
        self.meter = meter
        self._api_key = api_key
        self._llms: dict[str, Gemini] = {}

    def _llm(self, tier: str) -> Gemini:
        if tier not in self._llms:
            http_options = types.HttpOptions(
                timeout=config.CALL_TIMEOUT_MS[tier],
                retry_options=types.HttpRetryOptions(attempts=config.REQUEST_RETRY_ATTEMPTS, initial_delay=1.0),
            )
            client = genai.Client(api_key=self._api_key, http_options=http_options)
            self._llms[tier] = Gemini(model=config.TIERS[tier], client=client)
        return self._llms[tier]

    async def run_agent(
        self,
        *,
        step: str,
        tier: str,
        name: str,
        instruction: str,
        user_text: str,
        tools: list | None = None,
        output_schema: type[BaseModel] | None = None,
        deadline_s: float | None = None,
        thinking: str | None = "LOW",
        max_model_calls: int = 12,
    ) -> AgentResult:
        """Run one agent to its final answer. Raises RunStopped (budget, steps, retries, cancel) or CallFailed."""
        try:
            return await asyncio.wait_for(
                self._with_retries(step, tier, name, instruction, user_text, tools, output_schema, thinking, max_model_calls),
                timeout=deadline_s,
            )
        except asyncio.TimeoutError:  # inner timeouts are converted to CallFailed, so this is the deadline
            raise CallFailed("deadline", f"{name} did not finish within {deadline_s:.0f}s") from None

    async def _with_retries(self, step, tier, name, instruction, user_text, tools, output_schema, thinking, max_model_calls) -> AgentResult:
        last: CallFailed | None = None
        bad_output = 0
        for attempt in range(len(RETRY_BACKOFF_S) + 1):
            self.meter.check_alive()
            try:
                return await self._once(step, tier, name, instruction, user_text, tools, output_schema, thinking, max_model_calls)
            except RunStopped:
                raise
            except CallFailed as exc:
                last = exc
                if exc.reason != "bad_output":
                    raise
                bad_output += 1
                if bad_output > 1:  # a second unusable answer is not worth a third payment
                    raise
            except Exception as exc:  # noqa: BLE001 - classify, then retry or raise as CallFailed
                reason = _retryable(exc)
                if reason is None and type(exc).__name__ == "LlmCallsLimitExceededError":
                    raise CallFailed("limit", "used all its model rounds") from exc
                if reason is None:
                    log.warning("model call failed (%s): %s", name, safe_exc(exc))
                    raise CallFailed("provider", type(exc).__name__) from exc
                last = CallFailed(reason, type(exc).__name__)
            if attempt >= len(RETRY_BACKOFF_S):
                break
            self.meter.count_retry(f"{name}: {last.reason}")
            await asyncio.sleep(RETRY_BACKOFF_S[attempt])
        assert last is not None
        raise last

    async def _once(self, step, tier, name, instruction, user_text, tools, output_schema, thinking, max_model_calls) -> AgentResult:
        model_id = config.TIERS[tier]
        meter = self.meter
        outstanding: list[float] = []
        result = AgentResult()

        def before_model(callback_context, llm_request):
            try:
                outstanding.append(meter.before_call(step, tier, model_id, _request_chars(llm_request)))
            except RunStopped as stop:
                return LlmResponse(error_code=stop.reason.upper(), error_message=str(stop))
            return None

        def after_model(callback_context, llm_response):
            reserved = outstanding.pop() if outstanding else 0.0
            usage = llm_response.usage_metadata
            if usage is None:
                meter.release(reserved)
                return None
            t_in = (usage.prompt_token_count or 0) + (usage.tool_use_prompt_token_count or 0)
            t_out = (usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0)
            gm = llm_response.grounding_metadata
            queries = list(getattr(gm, "web_search_queries", None) or []) if gm else []
            meter.record(step, model_id, reserved, t_in, t_out, searches=len(queries))
            if gm:
                result.search_queries += queries
                for chunk in getattr(gm, "grounding_chunks", None) or []:
                    web = getattr(chunk, "web", None)
                    if web and web.uri:
                        result.grounding.append({"uri": web.uri, "title": web.title or ""})
            return None

        gen_cfg = types.GenerateContentConfig(
            max_output_tokens=config.MAX_OUTPUT_TOKENS[tier],
            temperature=0.4,
            thinking_config=types.ThinkingConfig(thinking_level=thinking) if thinking else None,
        )
        agent = LlmAgent(
            name=name,
            model=self._llm(tier),
            instruction=instruction,
            tools=tools or [],
            output_schema=output_schema,
            generate_content_config=gen_cfg,
            before_model_callback=before_model,
            after_model_callback=after_model,
        )
        session_service = InMemorySessionService()
        app_name, user_id, session_id = "daylight", "run", uuid.uuid4().hex
        runner = Runner(app_name=app_name, agent=agent, session_service=session_service)
        await session_service.create_session(app_name=app_name, user_id=user_id, session_id=session_id)
        final_text = ""
        failure: Exception | None = None  # raised after the stream ended: abandoning ADK's generator mid-way tears its tracing context down badly
        try:
            async for event in runner.run_async(
                user_id=user_id,
                session_id=session_id,
                new_message=types.Content(role="user", parts=[types.Part(text=user_text)]),
                run_config=RunConfig(max_llm_calls=max(1, min(meter.contract.max_steps, max_model_calls))),
            ):
                if event.error_code:
                    code = str(event.error_code).upper()
                    if code in _STOP_CODES:
                        meter.stop(_STOP_CODES[code], event.error_message or "")
                        failure = failure or RunStopped(_STOP_CODES[code], event.error_message or "")
                        continue
                    code = str(event.error_code)
                    log.warning("model call ended with %s: %s", code, scrub((event.error_message or "")[:200]))
                    if code == "LlmCallsLimitExceededError":
                        failure = failure or CallFailed("limit", "used all its model rounds")
                    elif code in _RETRY_BY_NAME:
                        failure = failure or _Retryable(_RETRY_BY_NAME[code], code)
                    else:
                        failure = failure or CallFailed("provider", code)
                    continue
                if event.is_final_response() and event.content and event.content.parts:
                    final_text = "".join(p.text or "" for p in event.content.parts if not getattr(p, "thought", False))
        finally:
            for reserved in outstanding:  # calls that never returned: give the reservation back
                meter.release(reserved)
            close = getattr(runner, "close", None)
            if close is not None:
                try:
                    await close()
                except Exception:  # noqa: BLE001 - closing is housekeeping; it must not hide the real outcome
                    pass
        if failure is not None:
            raise failure
        result.text = final_text
        if output_schema is not None:
            try:
                result.parsed = output_schema.model_validate_json(final_text)
            except (ValidationError, ValueError) as exc:
                raise CallFailed("bad_output", f"{type(exc).__name__}") from exc
        return result

"""A key never reaches a log. Every handler gets this filter; it rewrites anything that looks like a Google API key."""

import logging
import re

_KEY_RE = re.compile(r"AIza[0-9A-Za-z_\-]{20,}")
_BEARER_RE = re.compile(r"(?i)\b(bearer|x-goog-api-key|x-gemini-key)\b[\s:=]+\S+")


def scrub(text: str) -> str:
    return _BEARER_RE.sub(lambda m: f"{m.group(1)} [removed]", _KEY_RE.sub("[key removed]", text))


def scrub_record(record: logging.LogRecord) -> None:
    """Remove keys from a record and keep its shape: formatters like uvicorn's access log unpack record.args, so the
    args stay a tuple of the same length. Only when a key hides inside an argument that is not a string is the record
    collapsed into one finished, scrubbed message."""
    try:
        if isinstance(record.msg, str):
            record.msg = scrub(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(scrub(a) if isinstance(a, str) else a for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: scrub(v) if isinstance(v, str) else v for k, v in record.args.items()}
        message = record.getMessage()
        if scrub(message) != message:
            record.msg, record.args = scrub(message), ()
    except Exception:  # noqa: BLE001 - logging must never raise
        pass


class KeyScrubber(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        scrub_record(record)
        return True


_factory_installed = False


def install() -> None:
    """Idempotent. Call once at start-up of the web app, the night job and the CLI."""
    global _factory_installed
    if not _factory_installed:  # scrub at creation, so records of every logger are covered whatever handlers exist
        old = logging.getLogRecordFactory()

        def factory(*args, **kwargs):
            record = old(*args, **kwargs)
            scrub_record(record)
            return record

        logging.setLogRecordFactory(factory)
        old_format_exception = logging.Formatter.formatException
        logging.Formatter.formatException = lambda self, ei: scrub(old_format_exception(self, ei))  # tracebacks too
        _factory_installed = True
    root = logging.getLogger()
    if not any(isinstance(f, KeyScrubber) for f in root.filters):
        root.addFilter(KeyScrubber())
    for handler in root.handlers:
        if not any(isinstance(f, KeyScrubber) for f in handler.filters):
            handler.addFilter(KeyScrubber())
    quiet = logging.getLogger("google_adk")
    quiet.setLevel(logging.WARNING)
    for name in ("httpx", "httpcore", "google_genai", "google.adk"):
        logging.getLogger(name).setLevel(logging.WARNING)
    # ADK logs a stopped model call as a failed node, with a traceback; our own "daylight.*" lines say what really happened
    for name in ("google_adk.google.adk.workflow._node_runner", "google_adk.google.adk.runners"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    logging.getLogger("opentelemetry.context").setLevel(logging.CRITICAL)  # "created in a different Context": noise from ADK's tracing on cancel


def safe_exc(exc: BaseException) -> str:
    return scrub(f"{type(exc).__name__}: {str(exc)[:200]}")

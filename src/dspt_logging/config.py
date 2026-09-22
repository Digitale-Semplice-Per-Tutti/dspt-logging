"""One flat JSON object per record on stdout, and the noise around it turned off.

Fluent Bit ships container logs to a log platform with ``Merge_Log On``: every
top-level key of a JSON line becomes a searchable field there, while free text
only lands in ``message``. Lines are kept for two years, so a record carries
operational metadata only — never a citizen's name or codice fiscale, a request
payload, a prompt, a generated answer, a username or a vendor's response body.

The line
--------

Fixed keys, always present, in this order: ``timestamp`` (UTC, milliseconds,
``Z``), ``level``, ``logger``, ``service``, ``event`` (``application.log`` when
the call site named none), ``message``, ``tenant``. Then the logging context's
fields, if any are bound. Then the event's own fields in alphabetical order; an
explicit ``extra`` wins over the context. On an exception, ``error_type`` and
``stack_trace`` — the frames, never the exception's value, which may quote a
request body or a vendor's answer.

Numbers stay numbers, ``None`` stays ``null``, dicts and lists recurse, and
everything else becomes a string. **A measure nobody took is omitted**, never
sent as ``0`` or ``null``: zero is a plausible number and gets believed, and a
dashboard would average it into a percentile or sum it into a total.

Levels: ERROR = someone must look now. WARNING = it did not work and the system
held (a 5xx, a failed login, a failed vendor call, readiness down). INFO = a
fact worth counting, legitimate administrative actions included.

The noise
---------

Three things are silenced here rather than in each application's entrypoint,
because each of them cost a production incident somewhere in the fleet:

- ``uvicorn.access`` and ``gunicorn.access`` are disabled outright. The ASGI
  middleware in this package writes the access line, with the duration, the
  request id, the IP and the route the server's own line lacks. ``--no-access-log``
  is implemented as ``propagate = False``, so any code that later forces
  ``propagate = True`` on the server loggers re-enables it — which is how every
  request came to be logged twice in production.
- ``uvicorn``, ``uvicorn.error``, ``gunicorn`` and ``gunicorn.error`` keep no
  handlers of their own (they install text ones before the app is imported) and
  sit at WARNING: their start/stop narration is seven unlevelled lines per pod
  per rollout, times every tenant, saying nothing the application's own startup
  event does not. Their real failures are WARNING and above.
- the duplicate ``Exception in ASGI application`` traceback is dropped: the
  middleware already logged that exception as ``api.unhandled``.

An application that runs Alembic in-process must pass
``fileConfig(..., disable_existing_loggers=False)``, or Alembic's own logging
configuration will switch this one off.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import traceback
from collections.abc import MutableMapping
from datetime import UTC, datetime
from typing import Any, Final

from dspt_logging.context import current
from dspt_logging.events import (
    HTTP_ACCESS,
    LOGGING_LEVEL_INVALID,
    MIGRATION_APPLIED,
    PYTHON_WARNING,
    log_event,
)

#: What a line is called when the call site named nothing. A line with this
#: event is a line no dashboard can count: it is the checker's whole subject.
DEFAULT_EVENT: Final[str] = "application.log"

#: The keys the formatter owns, in the order they are written.
FIXED_KEYS: Final[tuple[str, ...]] = (
    "timestamp",
    "level",
    "logger",
    "service",
    "event",
    "message",
    "tenant",
)

_STANDARD_LOG_RECORD_ATTRS: Final[frozenset[str]] = frozenset(
    set(logging.makeLogRecord({}).__dict__) | {"message", "asctime", "stream", "color_message"}
)

# Libraries that narrate routine work at INFO: httpx prints the URL of every
# model call (naming the vendor endpoint on every turn), botocore announces
# where it found its credentials, APScheduler prints two lines per registered
# job. None of it is actionable; WARNING keeps their real failures.
QUIET_THIRD_PARTY_LOGGERS: Final[tuple[str, ...]] = (
    "httpx",
    "httpcore",
    "openai",
    "apscheduler",
    "botocore",
    "aiobotocore",
    "pymupdf",
)

_ACCESS_LOGGERS: Final[tuple[str, ...]] = ("uvicorn.access", "gunicorn.access")
_SERVER_LOGGERS: Final[tuple[str, ...]] = (
    "uvicorn",
    "uvicorn.error",
    "gunicorn",
    "gunicorn.error",
)

#: The attribute that tells this package's handler apart from a runtime's or a
#: test's. Never a class check and never "the first StreamHandler": both would
#: make a second call to :func:`configure_logging` fight with pytest.
_HANDLER_MARK: Final[str] = "_dspt_logging_json_handler"

_ALEMBIC_BOILERPLATE: Final[tuple[str, ...]] = ("Context impl ", "Will assume ")
_ALEMBIC_APPLIED: Final[tuple[str, ...]] = ("Running upgrade", "Running downgrade")


def _read_level(level: int | str | None) -> tuple[int, str | None]:
    """The level to run at, and the unreadable value it replaced, if any.

    ``LOG_LEVEL`` exists so one tenant can be put on DEBUG for an afternoon
    without a release. It is edited by hand in a ConfigMap, so a typo there
    (``VERBOSE``) must not take every pod of the deployment down at startup:
    the process runs at INFO and says so in its first line, at WARNING, where
    the dashboards look.
    """
    if level is None:
        from_env = os.getenv("LOG_LEVEL")
        level = from_env if from_env else logging.INFO
    if isinstance(level, str):
        resolved = logging.getLevelNamesMapping().get(level.strip().upper())
        if resolved is None:
            return logging.INFO, level
        return int(resolved), None
    return int(level), None


def resolve_log_level(level: int | str | None = None) -> int:
    """The level to run at: the argument, else ``LOG_LEVEL``, else INFO."""
    return _read_level(level)[0]


def _json_value(value: Any) -> Any:
    """Convert a structured field to a JSON-compatible value."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_value(item) for item in value]
    return str(value)


def _safe_log_args(value: Any) -> Any:
    """Replace exception values before %-formatting a human log message."""
    if isinstance(value, BaseException):
        return f"<{type(value).__name__}>"
    if isinstance(value, tuple):
        return tuple(_safe_log_args(item) for item in value)
    if isinstance(value, dict):
        return {key: _safe_log_args(item) for key, item in value.items()}
    return value


def _safe_message(record: logging.LogRecord) -> str:
    original_args = record.args
    try:
        record.args = _safe_log_args(original_args)
        message = record.getMessage()
    finally:
        record.args = original_args
    if record.exc_info and record.exc_info[0] is not None:
        exception_value = str(record.exc_info[1])
        if exception_value:
            message = message.replace(exception_value, f"<{record.exc_info[0].__name__}>")
    return message


def _access_message(record: logging.LogRecord) -> str:
    """Render an access record as ``GET /path 200 12ms`` from its fields.

    Text views, CSV exports and alert rules only ever see ``message``, so the
    essentials have to live there too; the fields stay for filtering.
    """
    parts = [
        getattr(record, "method", None),
        getattr(record, "path", None),
        getattr(record, "status_code", None),
    ]
    message = " ".join(str(part) for part in parts if part is not None)
    duration_ms = getattr(record, "duration_ms", None)
    if duration_ms is not None:
        message = f"{message} {duration_ms}ms".strip()
    return message or HTTP_ACCESS.message


class JsonFormatter(logging.Formatter):
    """Emit one flat JSON object per log record for Fluent Bit ``Merge_Log``."""

    def __init__(self, service: str, *, tenant: str | None = None) -> None:
        super().__init__()
        self._service = service
        self._tenant = tenant

    def format(self, record: logging.LogRecord) -> str:
        event = getattr(record, "event", DEFAULT_EVENT)
        timestamp = datetime.fromtimestamp(record.created, tz=UTC)
        payload: dict[str, Any] = {
            "timestamp": timestamp.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "service": self._service,
            "event": event,
            "message": (
                _access_message(record) if event == HTTP_ACCESS.name else _safe_message(record)
            ),
            "tenant": self._tenant,
        }

        # The context first, then the record's own fields: a line that names a
        # tenant or a job explicitly (a job walking the tenants) keeps saying
        # so over the context it runs in, and the key keeps the context's place
        # in the line so every record reads the same way.
        context = dict(current())
        if "tenant" in context:
            payload["tenant"] = context.pop("tenant")
        fields: dict[str, Any] = dict(context)
        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _STANDARD_LOG_RECORD_ATTRS and key not in FIXED_KEYS
        }
        for key in sorted(extras):
            fields[key] = extras[key]
        for key, value in fields.items():
            payload[key] = _json_value(value)

        if record.exc_info and record.exc_info[0] is not None:
            payload.setdefault("error_type", record.exc_info[0].__name__)
            # Call-site frames, never exception values: those carry a request
            # body, a codice fiscale or a raw vendor response.
            payload["stack_trace"] = "".join(traceback.format_tb(record.exc_info[2])).rstrip()

        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


class MergingLoggerAdapter(logging.LoggerAdapter[logging.Logger]):
    """Preserve adapter context while allowing per-call structured fields.

    ``LoggerAdapter`` normally *replaces* a call's ``extra`` with the adapter's
    context, so a long-running worker that binds an id loses every field its
    individual lines pass. Merge both mappings; per-call fields win on conflict.
    """

    def process(
        self, msg: Any, kwargs: MutableMapping[str, Any]
    ) -> tuple[Any, MutableMapping[str, Any]]:
        extra = dict(self.extra) if isinstance(self.extra, dict) else {}
        call_extra = kwargs.get("extra")
        if isinstance(call_extra, dict):
            extra.update(call_extra)
        kwargs["extra"] = extra
        return msg, kwargs


def _drop_uvicorn_duplicate_traceback(record: logging.LogRecord) -> bool:
    """The middleware already logged that exception as ``api.unhandled``."""
    return not record.getMessage().startswith("Exception in ASGI application")


def _alembic_filter(record: logging.LogRecord) -> bool:
    """Keep the one line that says what changed; drop the two that say nothing."""
    message = record.getMessage()
    if message.startswith(_ALEMBIC_BOILERPLATE):
        return False
    if message.startswith(_ALEMBIC_APPLIED):
        record.event = MIGRATION_APPLIED.name
    return True


def _tag_python_warning(record: logging.LogRecord) -> bool:
    record.event = PYTHON_WARNING.name
    return True


def _find_json_handler(logger: logging.Logger) -> logging.Handler | None:
    return next(
        (handler for handler in logger.handlers if getattr(handler, _HANDLER_MARK, False)),
        None,
    )


def _add_filter_once(logger: logging.Logger, log_filter: Any) -> None:
    if log_filter not in logger.filters:
        logger.addFilter(log_filter)


def configure_logging(
    service: str, *, tenant: str | None = None, level: int | str | None = None
) -> None:
    """Configure root logging once, at a process entrypoint.

    ``service`` is the process name every line carries (``<app>-backend``).
    ``tenant`` is the slug of the municipality this deployment serves, or
    ``None`` for a deployment that serves several — a line about one of them
    then binds ``tenant`` on the logging context instead.

    Repeated calls reuse the one handler this package owns and never remove
    handlers installed by the runtime or by tests. Application modules use
    ``logging.getLogger(__name__)`` and configure nothing themselves.
    """
    resolved, unreadable = _read_level(level)

    root = logging.getLogger()
    root.setLevel(resolved)

    handler = _find_json_handler(root)
    if handler is None:
        handler = logging.StreamHandler(sys.stdout)
        setattr(handler, _HANDLER_MARK, True)
        root.addHandler(handler)
    handler.setFormatter(JsonFormatter(service, tenant=tenant))
    handler.setLevel(resolved)

    for logger_name in QUIET_THIRD_PARTY_LOGGERS:
        logging.getLogger(logger_name).setLevel(logging.WARNING)

    for logger_name in _ACCESS_LOGGERS:
        access_logger = logging.getLogger(logger_name)
        access_logger.handlers.clear()
        access_logger.propagate = False
        access_logger.disabled = True

    for logger_name in _SERVER_LOGGERS:
        server_logger = logging.getLogger(logger_name)
        server_logger.handlers.clear()
        server_logger.setLevel(logging.WARNING)
        server_logger.propagate = True
        server_logger.disabled = False
        _add_filter_once(server_logger, _drop_uvicorn_duplicate_traceback)

    _add_filter_once(logging.getLogger("alembic.runtime.migration"), _alembic_filter)

    # Python warnings (SQLAlchemy's SAWarning, DeprecationWarning, ...)
    # otherwise go to stderr as plain text and bypass the JSON handler. Re-arm
    # on every call: test runners swap ``warnings.showwarning`` behind
    # logging's back.
    logging.captureWarnings(False)
    logging.captureWarnings(True)
    _add_filter_once(logging.getLogger("py.warnings"), _tag_python_warning)

    if unreadable is not None:
        log_event(logging.getLogger(__name__), LOGGING_LEVEL_INVALID, value=unreadable)

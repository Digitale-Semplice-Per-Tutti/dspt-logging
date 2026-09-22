"""The event catalogue: what a log line is called, says, and has to carry.

Every log line names an ``Event`` declared in an ``events.py`` and is emitted
through :func:`log_event`. Nothing is written as a string at the call site —
which is the point. A document describing the correct behaviour does not stop
an author who is pattern-matching on the code in front of them, and most log
lines are written that way. A constant that does not exist, and a CI step that
blocks, do.

Why it is worth the friction — every log line becomes two places to touch — is
that **a dashboard is a consumer that fails silently**. A broken route gets
noticed; a drifted contract keeps drawing a graph, and the graph is wrong.

The catalogue is **federated**: this module holds the mechanism, the shared
vocabulary and the five events the package emits by itself. Each application
holds its own ``events.py``. There is no central list, so this package never
has to know which applications exist.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Final

#: ``domain.object.action``, lowercase, at least two segments.
NAME_PATTERN: Final = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")

#: Field names used by events in more than one catalogue must be declared here,
#: with what they mean. The check is mechanical — is the name in this dict — but
#: its effect is human: to reuse one of these in a second application you have
#: to come and read the line, and that is where you notice your meaning differs.
#: ``method`` is the example the fleet paid for: one catalogue used it for the
#: request the API *made* and another for the one it *served*, and a panel
#: counting the 404s we answered counted a vendor's too.
#:
#: A few entries are here as documentation only: the formatter or the logging
#: context fills them, ``RESERVED_FIELDS`` forbids declaring them, and they are
#: listed so that the author who reaches for the name finds out why.
SHARED_FIELDS: Final[dict[str, str]] = {
    # --- the request the API served -----------------------------------------
    "method": "the HTTP method of the request the API SERVED",
    "status_code": "the status the API ANSWERED with",
    "path": "the path the API served, with secret and identifying segments masked",
    "route": "the route template the request matched; aggregates where `path` does not",
    "client_ip": "the address the edge asserted for the caller",
    "user_agent": "the caller's user agent, kept only where it earns its size",
    "code": "the machine-readable refusal code the API answered with",
    "invalid_fields": "which fields a request failed validation on, never their values",
    # --- the request the API made -------------------------------------------
    "upstream_method": "the HTTP method of the request the API MADE",
    "upstream_status_code": "the status an upstream ANSWERED us with",
    "upstream_host": "the host the API called",
    "eservice": "the vendor e-service slug; `service` is the process name the formatter adds",
    "operation_id": "the operation within an e-service or an API",
    # --- how it went ---------------------------------------------------------
    "duration_ms": "how long the thing this event describes took",
    "outcome": "how it ended, with a vocabulary of its own per event",
    "reason": "why, in words meant for a person reading the line",
    "action": "what was done, with a vocabulary of its own per event",
    "attempts": "how many tries the thing this event describes took",
    "suppressed": "how many identical lines this one stands for, when a line is rate-limited",
    "down_for_s": "how long the thing that just recovered had been failing",
    "version": "the release of the application the line was emitted by",
    # --- counts, never one line per element ----------------------------------
    "people_queued": "how many people this action put in a queue, at any scale",
    "certificates": "how many certificates this action handed over, at any scale",
    "recipient_count": "how many recipients a message was addressed to, at any scale",
    # --- AI calls ------------------------------------------------------------
    "model": "the AI deployment that answered",
    "feature": "the AI feature key the call served",
    "input_tokens": "tokens sent to the model",
    "output_tokens": "tokens the model produced",
    "cached_tokens": "input tokens the vendor billed as cached; absent when it reported nothing",
    "estimated_cost": "what the call cost in euro, omitted for a model with no published rate",
    # --- people and channels -------------------------------------------------
    "target_user_id": "the account an administrative action acted on",
    "channel": "which channel the citizen reached us on (web, whatsapp, ...)",
    # --- filled for you, listed so the name is not reused ---------------------
    "error_type": "RESERVED: the exception's class name, added by the formatter on exc_info",
    "job": "RESERVED: the periodic job, bound on the logging context by the runner",
}

#: Names no event may declare, because the formatter or the logging context
#: already owns them: the line's own keys, the six context fields, and the three
#: the formatter adds by itself. An event that declared ``request_id`` for a
#: vendor's identifier would pass the contract — the field *is* declared — and
#: then overwrite the HTTP request id on every line, which is exactly how a
#: vendor identifier once hid the request that caused each submission. Name the
#: thing you mean instead (``vendor_request_id``, ``target_user_id``).
RESERVED_FIELDS: Final[frozenset[str]] = frozenset(
    {
        # the line's fixed keys
        "timestamp",
        "level",
        "logger",
        "service",
        "event",
        "message",
        "tenant",
        # the logging context
        "request_id",
        "user_id",
        "job",
        "thread_id",
        "turn_id",
        # added by the formatter
        "stack_trace",
        "error_type",
        "contract_violation",
    }
)

#: Set by pytest for the duration of every test. Its presence is how a broken
#: line becomes a failure in development and a marked line in production.
_UNDER_TEST: Final[str] = "PYTEST_CURRENT_TEST"


@dataclass(frozen=True)
class Event:
    """One log line's contract.

    ``message`` lives here rather than at the call site so the sentence and the
    name cannot drift: a second place that emits the same event has nowhere to
    word it differently.
    """

    name: str
    message: str
    level: int = logging.INFO
    required: frozenset[str] = field(default_factory=frozenset)
    optional: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not NAME_PATTERN.match(self.name):
            raise ValueError(
                f"event name {self.name!r} is not `domain.object.action`: lowercase "
                "words separated by dots, at least two of them"
            )
        reserved = sorted(self.fields & RESERVED_FIELDS)
        if reserved:
            raise ValueError(
                f"event {self.name!r} declares {', '.join(reserved)}, which the formatter "
                "already fills: the logging context supplies request_id, user_id, tenant, "
                "job, thread_id and turn_id, and the line's own keys are not fields. Name "
                "the thing you mean (`vendor_request_id`, `target_user_id`)"
            )

    @property
    def fields(self) -> frozenset[str]:
        return self.required | self.optional


def log_event(
    logger: logging.Logger,
    event: Event,
    *,
    level: int | None = None,
    exc_info: bool | BaseException = False,
    **fields: Any,
) -> None:
    """Emit one line for ``event``.

    ``level`` overrides the catalogue's default, because severity is sometimes a
    property of the case rather than of the event: a refusal is INFO below 500
    and WARNING above, and a job's failure escalates at the fifth in a row. The
    catalogue says the usual answer; the call site says why this one differs.

    The six context fields are never passed here — the formatter adds them from
    the logging context, and no event should have to remember them.

    A line that breaks its contract **raises under pytest and never in
    production**: a missing field is a defect worth stopping the run that
    produced it, and never worth costing a request. In production the line goes
    out anyway carrying ``contract_violation``, so the defect is visible on the
    dashboard instead of being silently absent from it.
    """
    problems = _violations(event, fields)
    if problems:
        if os.environ.get(_UNDER_TEST):
            raise ValueError(f"{event.name}: {problems}")
        fields = {**fields, "contract_violation": problems}

    logger.log(
        event.level if level is None else level,
        event.message,
        extra={"event": event.name, **fields},
        exc_info=exc_info,
    )


def _violations(event: Event, fields: dict[str, Any]) -> str:
    missing = sorted(event.required - fields.keys())
    undeclared = sorted(fields.keys() - event.fields)
    parts = []
    if missing:
        parts.append(f"missing required field(s): {', '.join(missing)}")
    if undeclared:
        parts.append(f"field(s) the catalogue never declared: {', '.join(undeclared)}")
    return "; ".join(parts)


# --- the events this package emits itself -----------------------------------
#
# The only five. Everything else belongs to an application's own catalogue.
# The checker treats these as declared, so an application neither redeclares
# them nor writes their names by hand.

HTTP_ACCESS: Final = Event(
    name="http.access",
    message="HTTP access",
    required=frozenset({"method", "route", "path", "status_code", "duration_ms"}),
    optional=frozenset({"client_ip", "user_agent"}),
)

API_UNHANDLED: Final = Event(
    name="api.unhandled",
    message="Unhandled exception while serving a request",
    level=logging.ERROR,
    required=frozenset({"method", "route", "path"}),
)

PYTHON_WARNING: Final = Event(
    name="python.warning",
    message="Python warning",
    level=logging.WARNING,
)

MIGRATION_APPLIED: Final = Event(
    name="migration.applied",
    message="Database migration applied",
)

LOGGING_LEVEL_INVALID: Final = Event(
    name="logging.level.invalid",
    message="LOG_LEVEL is not a level name, running at INFO",
    level=logging.WARNING,
    required=frozenset({"value"}),
)

#: Every event this package owns, for the checker to seed its name table with.
PLATFORM_EVENTS: Final[tuple[Event, ...]] = (
    HTTP_ACCESS,
    API_UNHANDLED,
    PYTHON_WARNING,
    MIGRATION_APPLIED,
    LOGGING_LEVEL_INVALID,
)

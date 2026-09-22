"""What every log line should say about where it comes from.

Six context variables, set by the code that learns each fact and read by the
JSON formatter, so a line emitted anywhere in the process names them without
the emitter knowing. Nothing at a call site has to pass them along, which is
the point: the fact travels with the work rather than with the argument list.

Who binds what:

- ``request_id`` the ASGI middleware, once per request;
- ``user_id`` and ``tenant`` the application's authentication dependency;
- ``job`` the job runner, around one cycle;
- ``thread_id`` and ``turn_id`` the chat endpoints, as soon as they exist.

``user_id`` is the numeric id: a username is personal data and a log line is
kept for two years.

The field set is **closed**. A name outside it raises ``TypeError`` at once,
because a typo would otherwise bind something nothing ever reads, and a new
field is a decision for this module rather than for one call site: every name
here becomes a searchable field in the log index of six applications.

Values are ``contextvars``, so they follow ``asyncio`` tasks and
``asyncio.to_thread`` (both copy the current context) and survive an SSE
stream that yields for minutes. A bare ``loop.run_in_executor`` thread does
not inherit them.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any, Final

#: The only names that may be bound. Documented in ``SHARED_FIELDS`` too, so a
#: catalogue author reads why an event must not declare them.
FIELDS: Final[tuple[str, ...]] = (
    "request_id",
    "user_id",
    "tenant",
    "job",
    "thread_id",
    "turn_id",
)

_VARS: Final[dict[str, ContextVar[Any]]] = {
    name: ContextVar(f"dspt_log_{name}", default=None) for name in FIELDS
}

#: What :func:`bind` returns and :func:`unbind` takes back. Opaque on purpose:
#: a caller only ever hands it back.
BoundTokens = list[tuple[ContextVar[Any], Token[Any]]]


def _check(names: Iterator[str] | list[str]) -> None:
    unknown = sorted(set(names) - set(_VARS))
    if unknown:
        raise TypeError(
            f"unknown logging context field(s): {', '.join(unknown)}. "
            f"The set is closed: {', '.join(FIELDS)}"
        )


def bind(**fields: Any) -> BoundTokens:
    """Set context fields for the current task; returns tokens for :func:`unbind`."""
    _check(list(fields))
    return [(var, var.set(fields[name])) for name, var in _VARS.items() if name in fields]


def unbind(tokens: BoundTokens) -> None:
    """Undo one :func:`bind` call, innermost first."""
    for var, token in reversed(tokens):
        var.reset(token)


@contextmanager
def bound(**fields: Any) -> Iterator[None]:
    """Set the given fields for the duration of the block."""
    tokens = bind(**fields)
    try:
        yield
    finally:
        unbind(tokens)


def current() -> dict[str, Any]:
    """The fields that are set, in the order the formatter emits them."""
    return {name: value for name, var in _VARS.items() if (value := var.get()) is not None}

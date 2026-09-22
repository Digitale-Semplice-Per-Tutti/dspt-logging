"""What an application's own logging tests need from this package.

Registered as a pytest plugin through the ``pytest11`` entry point, so
installing ``dspt-logging`` is enough: no ``pytest_plugins`` line to remember
in a ``conftest.py``.

Two things, both for the tests that stay in the applications — the privacy
tests above all, which are the ones that must not be centralised: only the
application knows what its own secrets look like.
"""

from __future__ import annotations

import logging
import traceback
from collections.abc import Iterator, Sequence
from typing import Any

import pytest

from dspt_logging.config import _STANDARD_LOG_RECORD_ATTRS, FIXED_KEYS


class _ListHandler(logging.Handler):
    """Keep the records themselves, not their rendered text.

    A test asserts on fields (``record.status_code``), and rendering first
    would throw away everything but ``message``.
    """

    def __init__(self, records: list[logging.LogRecord]) -> None:
        super().__init__(level=logging.NOTSET)
        self._records = records

    def emit(self, record: logging.LogRecord) -> None:
        self._records.append(record)


@pytest.fixture
def log_records() -> Iterator[list[logging.LogRecord]]:
    """Every record that reaches the root logger during the test.

    The root level is lowered to DEBUG for the duration and restored after, so
    a test can assert on the lines an application deliberately writes at DEBUG
    (a scanner's 404) without configuring anything.
    """
    records: list[logging.LogRecord] = []
    handler = _ListHandler(records)
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)


def _fields(record: logging.LogRecord) -> dict[str, Any]:
    return {
        key: value
        for key, value in record.__dict__.items()
        if key not in _STANDARD_LOG_RECORD_ATTRS and key not in FIXED_KEYS
    }


def _message(record: logging.LogRecord) -> str:
    try:
        return record.getMessage()
    except Exception:  # pragma: no cover - a malformed call site, not our subject
        return str(record.msg)


def _stack(record: logging.LogRecord) -> str:
    if not record.exc_info or record.exc_info[0] is None:
        return ""
    return "".join(traceback.format_tb(record.exc_info[2]))


def assert_no_secret(records: Sequence[logging.LogRecord], secret: str) -> None:
    """Fail if ``secret`` appears anywhere in these records.

    Message, every structured field (nested values included) and the stack
    frames. Logs are kept for two years, so "it is only in the stack trace" is
    not a mitigation: a citizen's codice fiscale, a prompt, an answer or a
    vendor's response body must not be there at all.
    """
    if not secret:
        raise ValueError("assert_no_secret() needs a non-empty secret to look for")
    for record in records:
        message = _message(record)
        if secret in message:
            raise AssertionError(
                f"{record.name}: the secret is in the message of {getattr(record, 'event', '?')}"
            )
        for name, value in _fields(record).items():
            if secret in str(value):
                raise AssertionError(
                    f"{record.name}: the secret is in the field {name!r} of "
                    f"{getattr(record, 'event', '?')}"
                )
        if secret in _stack(record):
            raise AssertionError(
                f"{record.name}: the secret is in the stack trace of "
                f"{getattr(record, 'event', '?')} — log the exception's type, never its value"
            )

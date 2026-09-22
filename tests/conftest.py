"""The root logger is process-wide state, so every test puts it back.

``json_lines`` is the fixture most of these tests want: it configures logging
the way an application does, sends the JSON handler to a buffer, and hands back
a function returning the parsed records.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from dspt_logging.config import _find_json_handler, configure_logging

SERVICE = "example-backend"


@pytest.fixture(autouse=True)
def restore_root_logger() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    try:
        yield
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)


@pytest.fixture(autouse=True)
def no_log_level_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's LOG_LEVEL must not decide what these tests see."""
    monkeypatch.delenv("LOG_LEVEL", raising=False)


@pytest.fixture
def json_lines() -> Iterator[Callable[[], list[dict[str, Any]]]]:
    configure_logging(SERVICE)
    handler = _find_json_handler(logging.getLogger())
    assert handler is not None
    stream = io.StringIO()
    original = handler.stream
    handler.stream = stream  # type: ignore[attr-defined]

    def records() -> list[dict[str, Any]]:
        return [json.loads(line) for line in stream.getvalue().splitlines() if line]

    try:
        yield records
    finally:
        handler.stream = original  # type: ignore[attr-defined]

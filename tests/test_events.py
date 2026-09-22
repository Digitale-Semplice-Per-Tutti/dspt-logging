"""A log line's name, sentence, level and fields, declared once."""

from __future__ import annotations

import logging

import pytest

from dspt_logging.events import (
    API_UNHANDLED,
    HTTP_ACCESS,
    MIGRATION_APPLIED,
    PYTHON_WARNING,
    RESERVED_FIELDS,
    SHARED_FIELDS,
    Event,
    log_event,
)

# `log_records` needs no import: the package registers it as a pytest plugin.
SENT = Event(
    name="test.thing.sent",
    message="Thing sent",
    required=frozenset({"thing_id"}),
    optional=frozenset({"attempts"}),
)

FAILED = Event(
    name="test.thing.failed",
    message="Thing not sent",
    level=logging.WARNING,
    required=frozenset({"thing_id"}),
)


def _only(records: list[logging.LogRecord]) -> logging.LogRecord:
    (record,) = [item for item in records if getattr(item, "event", "")]
    return record


def test_the_line_carries_the_name_sentence_and_level_from_the_catalogue(
    log_records: list[logging.LogRecord],
) -> None:
    """The sentence lives with the name, so the two cannot drift: a second call
    site cannot word it differently, because there is nowhere to word it."""
    log_event(logging.getLogger("test.emitter"), SENT, thing_id=7)

    record = _only(log_records)
    assert record.event == "test.thing.sent"  # type: ignore[attr-defined]
    assert record.getMessage() == "Thing sent"
    assert record.levelno == logging.INFO
    assert record.thing_id == 7  # type: ignore[attr-defined]


def test_the_call_site_may_raise_the_level(log_records: list[logging.LogRecord]) -> None:
    """Severity is sometimes a property of the case: a refusal is INFO below
    500 and WARNING above, and a job's failure escalates at the fifth."""
    logger = logging.getLogger("test.emitter")
    log_event(logger, FAILED, thing_id=7)
    log_event(logger, FAILED, level=logging.ERROR, thing_id=7)

    assert [record.levelno for record in log_records] == [logging.WARNING, logging.ERROR]


def test_an_optional_field_may_be_left_out(log_records: list[logging.LogRecord]) -> None:
    log_event(logging.getLogger("test.emitter"), SENT, thing_id=7)
    assert not hasattr(_only(log_records), "attempts")


def test_a_missing_required_field_fails_the_test_that_produced_it() -> None:
    with pytest.raises(ValueError, match="thing_id"):
        log_event(logging.getLogger("test.emitter"), SENT)


def test_a_field_the_catalogue_never_declared_is_refused() -> None:
    """The catalogue is the documentation a dashboard is built from."""
    with pytest.raises(ValueError, match="surprise"):
        log_event(logging.getLogger("test.emitter"), SENT, thing_id=7, surprise="hello")


def test_in_production_the_broken_line_goes_out_anyway_marked(
    monkeypatch: pytest.MonkeyPatch, log_records: list[logging.LogRecord]
) -> None:
    """A log call must never break the work it describes. The line goes out
    marked, so the defect is visible on the dashboard rather than costing a
    request."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    log_event(logging.getLogger("test.emitter"), SENT)

    record = _only(log_records)
    assert record.event == "test.thing.sent"  # type: ignore[attr-defined]
    assert "thing_id" in record.contract_violation  # type: ignore[attr-defined]


def test_an_exception_is_carried_through_to_the_formatter(
    log_records: list[logging.LogRecord],
) -> None:
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        log_event(logging.getLogger("test.emitter"), FAILED, exc_info=True, thing_id=7)

    assert _only(log_records).exc_info is not None


def test_an_event_name_must_be_a_dotted_lowercase_identifier() -> None:
    with pytest.raises(ValueError, match="Thing.Sent"):
        Event(name="Thing.Sent", message="x")
    with pytest.raises(ValueError, match="sent"):
        Event(name="sent", message="x")


@pytest.mark.parametrize(
    "name", ["request_id", "user_id", "tenant", "job", "message", "timestamp", "error_type"]
)
def test_a_field_the_formatter_owns_cannot_be_declared(name: str) -> None:
    """Declared for a vendor's identifier, `request_id` would pass the contract
    and then overwrite the HTTP request id on every line."""
    with pytest.raises(ValueError, match=name):
        Event(name="test.thing.sent", message="Sent", required=frozenset({name}))


def test_the_reserved_names_are_the_fixed_keys_the_context_and_the_formatters_own() -> None:
    assert {"timestamp", "level", "logger", "service", "event", "message", "tenant"} <= (
        RESERVED_FIELDS
    )
    assert {"request_id", "user_id", "job", "thread_id", "turn_id"} <= RESERVED_FIELDS
    assert {"stack_trace", "error_type", "contract_violation"} <= RESERVED_FIELDS


def test_the_platform_events_are_the_only_four_and_carry_their_fields() -> None:
    assert HTTP_ACCESS.name == "http.access"
    assert API_UNHANDLED.level == logging.ERROR
    assert PYTHON_WARNING.name == "python.warning"
    assert MIGRATION_APPLIED.name == "migration.applied"
    assert {"method", "route", "path", "status_code", "duration_ms"} <= HTTP_ACCESS.required


def test_every_platform_field_is_in_the_shared_vocabulary() -> None:
    """A field this package emits is by definition used in more than one
    application, so the checker must find it declared."""
    for event in (HTTP_ACCESS, API_UNHANDLED):
        assert event.fields <= set(SHARED_FIELDS)

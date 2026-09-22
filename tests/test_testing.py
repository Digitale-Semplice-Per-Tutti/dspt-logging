"""The two helpers an application's own logging tests are written with."""

from __future__ import annotations

import logging

import pytest

from dspt_logging.testing import assert_no_secret


def test_the_fixture_captures_records_with_their_fields(
    log_records: list[logging.LogRecord],
) -> None:
    logging.getLogger("test.capture").info(
        "Appointment booked", extra={"event": "appointment.booking.completed", "channel": "web"}
    )

    (record,) = log_records
    assert record.event == "appointment.booking.completed"  # type: ignore[attr-defined]
    assert record.channel == "web"  # type: ignore[attr-defined]


def test_the_fixture_sees_debug_lines_too(log_records: list[logging.LogRecord]) -> None:
    """An application deliberately writes a scanner's 404 at DEBUG, and its
    test has to be able to assert that."""
    logging.getLogger("test.debug").debug("quiet line")

    assert [record.levelno for record in log_records] == [logging.DEBUG]


def test_a_clean_run_passes(log_records: list[logging.LogRecord]) -> None:
    logging.getLogger("test.clean").info("Appointment booked", extra={"channel": "web"})

    assert_no_secret(log_records, "Via Roma 1")


def test_a_secret_in_the_message_is_caught(log_records: list[logging.LogRecord]) -> None:
    logging.getLogger("test.leak").info("Answer: %s", "Via Roma 1")

    with pytest.raises(AssertionError, match="message"):
        assert_no_secret(log_records, "Via Roma 1")


def test_a_secret_in_a_field_is_caught(log_records: list[logging.LogRecord]) -> None:
    logging.getLogger("test.leak").info("Answer sent", extra={"sources": ["Via Roma 1"]})

    with pytest.raises(AssertionError, match="sources"):
        assert_no_secret(log_records, "Via Roma 1")


def test_a_secret_in_the_stack_is_caught(log_records: list[logging.LogRecord]) -> None:
    """ "It is only in the stack trace" is not a mitigation: the line is kept
    for two years."""

    def read_the_citizens_address() -> None:
        raise RuntimeError("boom")

    try:
        read_the_citizens_address()
    except RuntimeError:
        logging.getLogger("test.leak").exception("Failed")

    with pytest.raises(AssertionError, match="stack trace"):
        assert_no_secret(log_records, "read_the_citizens_address")


def test_an_empty_secret_is_a_mistake(log_records: list[logging.LogRecord]) -> None:
    with pytest.raises(ValueError):
        assert_no_secret(log_records, "")

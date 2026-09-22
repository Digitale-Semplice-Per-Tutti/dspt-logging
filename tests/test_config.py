"""The line's contract, and the noise configure_logging() turns off."""

from __future__ import annotations

import io
import json
import logging
import warnings
from collections.abc import Callable
from typing import Any

import pytest

from dspt_logging.config import (
    FIXED_KEYS,
    MergingLoggerAdapter,
    _find_json_handler,
    configure_logging,
    resolve_log_level,
)

Records = Callable[[], list[dict[str, Any]]]


class TestSetup:
    def test_adds_one_stdout_handler_when_there_is_none(self) -> None:
        root = logging.getLogger()
        root.handlers.clear()
        root.setLevel(logging.WARNING)

        configure_logging("example-backend")

        assert len(root.handlers) == 1
        assert root.level == logging.INFO

    def test_is_idempotent(self) -> None:
        root = logging.getLogger()
        root.handlers.clear()

        configure_logging("example-backend")
        first = [id(handler) for handler in root.handlers]
        configure_logging("example-backend")

        assert [id(handler) for handler in root.handlers] == first

    def test_does_not_remove_unrelated_root_handlers(self) -> None:
        """pytest and the runtime install handlers of their own."""
        root = logging.getLogger()
        unrelated = logging.NullHandler()
        root.addHandler(unrelated)

        configure_logging("example-backend")

        assert unrelated in root.handlers

    def test_the_log_level_environment_variable_is_the_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One tenant can be put on DEBUG for an afternoon without a release."""
        monkeypatch.setenv("LOG_LEVEL", "debug")

        configure_logging("example-backend")

        assert logging.getLogger().level == logging.DEBUG

    def test_an_explicit_level_wins_over_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LOG_LEVEL", "debug")

        configure_logging("example-backend", level=logging.WARNING)

        assert logging.getLogger().level == logging.WARNING

    def test_an_unreadable_level_runs_at_info_and_says_so(
        self, monkeypatch: pytest.MonkeyPatch, log_records: list[logging.LogRecord]
    ) -> None:
        """LOG_LEVEL is typed by hand in a ConfigMap: a typo must not take every
        pod down at startup, and must not pass in silence either."""
        monkeypatch.setenv("LOG_LEVEL", "verbose")

        configure_logging("example-backend")

        assert logging.getLogger().level == logging.INFO
        complaints = [r for r in log_records if r.event == "logging.level.invalid"]  # type: ignore[attr-defined]
        assert len(complaints) == 1
        assert complaints[0].levelno == logging.WARNING
        assert complaints[0].value == "verbose"  # type: ignore[attr-defined]

    def test_resolve_log_level_accepts_a_name_a_number_or_nothing(self) -> None:
        assert resolve_log_level("WARNING") == logging.WARNING
        assert resolve_log_level(logging.DEBUG) == logging.DEBUG
        assert resolve_log_level() == logging.INFO


class TestTheLine:
    def test_the_fixed_keys_come_first_and_in_order(self, json_lines: Records) -> None:
        logging.getLogger("test.order").info(
            "Appointment booked", extra={"event": "appointment.booking.completed", "channel": "web"}
        )

        record = json_lines()[0]
        assert tuple(record)[: len(FIXED_KEYS)] == FIXED_KEYS
        assert record["timestamp"].endswith("Z")
        assert record["service"] == "example-backend"
        assert record["event"] == "appointment.booking.completed"
        assert record["channel"] == "web"

    def test_a_line_nobody_named_is_an_application_log(self, json_lines: Records) -> None:
        logging.getLogger("test.plain").info("plain %s line", "info")

        record = json_lines()[0]
        assert record["event"] == "application.log"
        assert record["message"] == "plain info line"

    def test_the_process_tenant_is_on_every_line(self) -> None:
        configure_logging("example-backend", tenant="tenant-a")
        handler = _find_json_handler(logging.getLogger())
        assert handler is not None
        stream = io.StringIO()
        handler.stream = stream  # type: ignore[attr-defined]

        logging.getLogger("test.tenant").info("hello")

        assert json.loads(stream.getvalue())["tenant"] == "tenant-a"

    def test_numbers_stay_numbers_and_anything_else_becomes_a_string(
        self, json_lines: Records
    ) -> None:
        logging.getLogger("test.types").info(
            "typed fields",
            extra={"duration_ms": 12, "estimated_cost": 0.5, "when": object(), "tags": ("a", "b")},
        )

        record = json_lines()[0]
        assert record["duration_ms"] == 12
        assert record["estimated_cost"] == 0.5
        assert isinstance(record["when"], str)
        assert record["tags"] == ["a", "b"]

    def test_a_field_that_was_never_measured_is_absent_and_an_unknown_one_is_null(
        self, json_lines: Records
    ) -> None:
        """Zero is a plausible number and gets believed; absent is honest."""
        logging.getLogger("test.absent").info("cost", extra={"cached_tokens": None})

        record = json_lines()[0]
        assert record["cached_tokens"] is None
        assert "estimated_cost" not in record

    def test_the_fields_are_in_alphabetical_order(self, json_lines: Records) -> None:
        logging.getLogger("test.sorted").info("sorted", extra={"zulu": 1, "alpha": 2, "mike": 3})

        record = json_lines()[0]
        assert [key for key in record if key not in FIXED_KEYS] == ["alpha", "mike", "zulu"]

    def test_the_access_line_says_what_happened_in_its_message(self, json_lines: Records) -> None:
        """Text views, CSV exports and alert rules only see the message."""
        logging.getLogger("test.access").info(
            "HTTP access",
            extra={
                "event": "http.access",
                "method": "GET",
                "path": "/api/appointments",
                "status_code": 200,
                "duration_ms": 12,
            },
        )

        assert json_lines()[0]["message"] == "GET /api/appointments 200 12ms"

    def test_an_exception_keeps_its_type_and_frames_and_loses_its_value(
        self, json_lines: Records
    ) -> None:
        secret = "citizen-address-in-error"
        try:
            raise RuntimeError(secret)
        except RuntimeError:
            logging.getLogger("test.exception").exception("Vendor call failed: %s", "see exception")

        record = json_lines()[0]
        assert record["error_type"] == "RuntimeError"
        assert "test_config.py" in record["stack_trace"]
        assert secret not in json.dumps(json_lines())

    def test_an_exception_passed_as_an_argument_is_redacted(self, json_lines: Records) -> None:
        secret = "raw-vendor-body-do-not-log"
        logging.getLogger("test.argument").warning("Downstream failure: %s", RuntimeError(secret))

        record = json_lines()[0]
        assert record["message"] == "Downstream failure: <RuntimeError>"
        assert secret not in json.dumps(json_lines())

    def test_the_exception_value_is_cut_out_of_the_message_too(self, json_lines: Records) -> None:
        secret = "raw-vendor-body"
        try:
            raise RuntimeError(secret)
        except RuntimeError:
            logging.getLogger("test.exception.message").exception("Vendor call failed: %s", secret)

        assert json_lines()[0]["message"] == "Vendor call failed: <RuntimeError>"


class TestTheNoise:
    def test_the_servers_own_access_log_never_reaches_stdout(self, json_lines: Records) -> None:
        """--no-access-log is `propagate = False`, so anything that turns
        propagation back on logs every request twice."""
        for name in ("uvicorn.access", "gunicorn.access"):
            access_logger = logging.getLogger(name)
            access_logger.propagate = True
            access_logger.disabled = False
            configure_logging("example-backend")
            access_logger.info(
                '%s - "%s %s HTTP/%s" %d', "127.0.0.1", "GET", "/api/ping?token=secret", "1.1", 200
            )

        assert json_lines() == []

    def test_the_servers_start_and_stop_narration_is_dropped_but_its_failures_pass(
        self, json_lines: Records
    ) -> None:
        server = logging.getLogger("uvicorn.error")
        server.info("Started server process [1]")
        server.info("Application startup complete.")
        server.warning("Invalid HTTP request received.")

        records = json_lines()
        assert [record["message"] for record in records] == ["Invalid HTTP request received."]
        assert records[0]["logger"] == "uvicorn.error"

    def test_the_duplicate_asgi_traceback_is_dropped(self, json_lines: Records) -> None:
        """The middleware already logged that exception as api.unhandled."""
        logging.getLogger("uvicorn.error").error("Exception in ASGI application")

        assert json_lines() == []

    def test_third_party_chatter_is_quiet_but_their_warnings_pass(
        self, json_lines: Records
    ) -> None:
        for name in ("httpx", "openai", "botocore", "apscheduler", "pymupdf"):
            logging.getLogger(name).info(
                'HTTP Request: POST https://vendor.example/v1 "HTTP/1.1 200 OK"'
            )
            logging.getLogger(name).warning("vendor trouble")

        records = json_lines()
        assert [record["logger"] for record in records] == [
            "httpx",
            "openai",
            "botocore",
            "apscheduler",
            "pymupdf",
        ]
        assert all(record["level"] == "WARNING" for record in records)
        assert "vendor.example" not in json.dumps(records)

    def test_python_warnings_arrive_as_json_with_a_name(self, json_lines: Records) -> None:
        warnings.warn("cartesian product between FROM elements", UserWarning, stacklevel=1)

        record = json_lines()[0]
        assert record["logger"] == "py.warnings"
        assert record["level"] == "WARNING"
        assert record["event"] == "python.warning"
        assert "cartesian product" in record["message"]

    def test_alembic_boilerplate_goes_and_the_migration_is_named(self, json_lines: Records) -> None:
        alembic = logging.getLogger("alembic.runtime.migration")
        alembic.info("Context impl PostgresqlImpl.")
        alembic.info("Will assume transactional DDL.")
        alembic.info("Running upgrade 0006 -> 0007, drop the call log")

        (record,) = json_lines()
        assert record["event"] == "migration.applied"
        assert "0007" in record["message"]


def test_the_adapter_merges_its_context_with_the_calls_fields(json_lines: Records) -> None:
    """LoggerAdapter replaces `extra` by default, so a worker that binds an id
    would lose every field its individual lines pass."""
    adapter = MergingLoggerAdapter(logging.getLogger("test.merge"), {"job": "nightly"})

    adapter.info("background line", extra={"event": "job.output", "duration_ms": 3})

    record = json_lines()[0]
    assert record["job"] == "nightly"
    assert record["duration_ms"] == 3
    assert record["event"] == "job.output"

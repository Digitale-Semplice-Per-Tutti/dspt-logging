"""The access line, end to end, through a real application and a real client."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from dspt_logging import configure_logging, context
from dspt_logging.asgi import (
    _incoming_request_id,
    install_request_logging,
    request_identity,
)

Records = Callable[[], list[dict[str, Any]]]

# In a variable, as a real value would be: `traceback.format_tb` prints the
# source line of every frame, so a literal in the raising line would land in
# `stack_trace` no matter what the formatter does with the exception's value.
CITIZEN_IDENTIFIER = "RSSMRA80A01H501U"


def _app(**options: Any) -> FastAPI:
    app = FastAPI()
    install_request_logging(app, **options)

    @app.get("/api/ping")
    async def ping() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/ready")
    async def ready() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/tenants/{slug}/enter")
    async def enter(slug: str) -> dict[str, str]:
        return {"slug": slug}

    @app.get("/api/people/{codice_fiscale}/certificate")
    async def certificate(codice_fiscale: str) -> dict[str, bool]:
        return {"ok": True}

    @app.get("/api/activate/{token}/info")
    async def activate(token: str) -> dict[str, bool]:
        return {"ok": True}

    @app.get("/api/boom")
    async def boom() -> None:
        raise RuntimeError(CITIZEN_IDENTIFIER)

    @app.get("/api/inside")
    async def inside() -> dict[str, bool]:
        logging.getLogger("test.handler").info("inside the handler")
        return {"ok": True}

    @app.get("/api/stream")
    async def stream() -> StreamingResponse:
        async def chunks() -> AsyncIterator[bytes]:
            for _ in range(3):
                logging.getLogger("test.stream").info(
                    "chunk", extra={"seen": context.current().get("request_id")}
                )
                yield b"data: x\n\n"

        return StreamingResponse(chunks(), media_type="text/event-stream")

    return app


async def _get(
    app: FastAPI,
    path: str,
    headers: dict[str, str] | None = None,
    raise_app_exceptions: bool = True,
) -> httpx.Response:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions)
    async with httpx.AsyncClient(transport=transport, base_url="http://example.test") as client:
        return await client.get(path, headers=headers)


def _access(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [record for record in records if record["event"] == "http.access"]


async def test_the_access_line_has_the_requests_fields(json_lines: Records) -> None:
    response = await _get(_app(), "/api/ping")

    assert response.status_code == 200
    (line,) = _access(json_lines())
    assert line["message"].startswith("GET /api/ping 200 ")
    assert line["message"].endswith("ms")
    assert line["method"] == "GET"
    assert line["route"] == "/api/ping"
    assert line["path"] == "/api/ping"
    assert line["status_code"] == 200
    assert isinstance(line["duration_ms"], int)
    assert line["request_id"] == response.headers["x-request-id"]
    assert len(line["request_id"]) >= 8


async def test_a_callers_request_id_is_kept_and_echoed(json_lines: Records) -> None:
    response = await _get(_app(), "/api/ping", headers={"X-Request-ID": "front-123"})

    assert response.headers["x-request-id"] == "front-123"
    assert _access(json_lines())[0]["request_id"] == "front-123"


@pytest.mark.parametrize("offered", ["", "x" * 65])
async def test_an_unusable_request_id_is_replaced(json_lines: Records, offered: str) -> None:
    """The header is caller-controlled and lands in a searchable field, so it
    is taken only when it is bounded and printable."""
    response = await _get(_app(), "/api/ping", headers={"X-Request-ID": offered})

    assert response.headers["x-request-id"] != offered
    assert _access(json_lines())[0]["request_id"] == response.headers["x-request-id"]


def test_an_unprintable_request_id_is_refused() -> None:
    scope = {"type": "http", "headers": [(b"x-request-id", b"bad\x01id")]}
    assert _incoming_request_id(scope) is None


async def test_the_forwarded_address_wins_over_the_socket(json_lines: Records) -> None:
    await _get(
        _app(),
        "/api/ping",
        headers={"X-Forwarded-For": "203.0.113.9, 198.51.100.1", "X-Real-IP": "198.51.100.7"},
    )

    assert _access(json_lines())[0]["client_ip"] == "203.0.113.9"


async def test_x_real_ip_is_the_fallback(json_lines: Records) -> None:
    await _get(_app(), "/api/ping", headers={"X-Real-IP": "198.51.100.7"})

    assert _access(json_lines())[0]["client_ip"] == "198.51.100.7"


async def test_the_user_agent_is_truncated(json_lines: Records) -> None:
    await _get(_app(), "/api/ping", headers={"User-Agent": "x" * 500})

    assert _access(json_lines())[0]["user_agent"] == "x" * 200


async def test_the_route_template_is_kept_and_the_path_is_not_masked_by_default(
    json_lines: Records,
) -> None:
    await _get(_app(), "/api/tenants/tenant-a/enter")

    (line,) = _access(json_lines())
    assert line["route"] == "/api/tenants/{slug}/enter"
    assert line["path"] == "/api/tenants/tenant-a/enter"


@pytest.mark.parametrize(
    ("path", "route", "masked"),
    [
        (
            f"/api/people/{CITIZEN_IDENTIFIER}/certificate",
            "/api/people/{codice_fiscale}/certificate",
            "/api/people/*/certificate",
        ),
        (
            "/api/activate/SECRET-TOKEN/info",
            "/api/activate/{token}/info",
            "/api/activate/*/info",
        ),
    ],
)
async def test_an_identifying_path_parameter_is_masked(
    json_lines: Records, path: str, route: str, masked: str
) -> None:
    """A codice fiscale in the path is a citizen's identifier in a field kept
    for two years; `route` keeps the template so the line stays aggregable."""
    await _get(_app(), path)

    (line,) = _access(json_lines())
    assert line["route"] == route
    assert line["path"] == masked
    assert CITIZEN_IDENTIFIER not in json.dumps(json_lines())
    assert "SECRET-TOKEN" not in json.dumps(json_lines())


async def test_the_query_string_is_never_logged(json_lines: Records) -> None:
    await _get(_app(), "/api/ping?token=do-not-log")

    assert "do-not-log" not in json.dumps(json_lines())


async def test_a_successful_probe_leaves_no_line(json_lines: Records) -> None:
    await _get(_app(), "/api/health")

    assert _access(json_lines()) == []


async def test_a_self_probe_recognised_by_its_user_agent_leaves_no_line(
    json_lines: Records,
) -> None:
    """An application that probes one of its own public routes (a webhook
    handshake, every couple of minutes, per tenant) cannot name the route as a
    probe: real callers use it too. It names itself in the user agent instead,
    and those lines are silent whatever they answered."""
    app = _app(silent_user_agents=("example-internal-probe",))

    await _get(app, "/api/ping", headers={"User-Agent": "example-internal-probe/webhook"})
    await _get(app, "/api/ping", headers={"User-Agent": "Mozilla/5.0"})

    (line,) = _access(json_lines())
    assert line["user_agent"] == "Mozilla/5.0"


async def test_a_failing_probe_is_logged(json_lines: Records) -> None:
    """The rule is not "probes are quiet": a 200 says nothing, a 503 says
    everything."""
    app = _app()

    @app.get("/metrics/_probe")
    async def probe() -> None:
        raise ValueError("down")

    await _get(app, "/metrics/_probe", raise_app_exceptions=False)

    assert _access(json_lines())[0]["status_code"] == 500


async def test_readiness_is_silent_even_when_it_fails(json_lines: Records) -> None:
    """The readiness route keeps its own rate-limited record; an access line
    per probe undid that limit exactly when it mattered."""
    app = _app()

    @app.get("/ready")
    async def ready() -> None:
        raise ValueError("down")

    await _get(app, "/ready", raise_app_exceptions=False)
    await _get(app, "/api/ready")

    assert _access(json_lines()) == []


async def test_a_scanners_404_is_debug_while_the_applications_own_stays_info(
    json_lines: Records,
) -> None:
    # At INFO the scanner's line is not written at all, which is the point of
    # the option; DEBUG is where the difference between the two is visible.
    configure_logging("example-backend", level=logging.DEBUG)
    app = _app(quiet_404_outside=("/api", "/static"))

    await _get(app, "/xmlrpc.php")
    await _get(app, "/api/nope")

    by_path = {record["path"]: record for record in _access(json_lines())}
    assert by_path["/api/nope"]["level"] == "INFO"
    assert by_path["/xmlrpc.php"]["level"] == "DEBUG"


async def test_without_the_option_every_404_stays_info(json_lines: Records) -> None:
    await _get(_app(), "/xmlrpc.php")

    assert _access(json_lines())[0]["level"] == "INFO"


async def test_an_unhandled_exception_is_named_and_re_raised(json_lines: Records) -> None:
    with pytest.raises(RuntimeError):
        await _get(_app(), "/api/boom")

    records = json_lines()
    (unhandled,) = [record for record in records if record["event"] == "api.unhandled"]
    assert unhandled["level"] == "ERROR"
    assert unhandled["error_type"] == "RuntimeError"
    assert unhandled["route"] == "/api/boom"
    assert "stack_trace" in unhandled
    # The stack keeps source lines, never the exception's value.
    assert CITIZEN_IDENTIFIER not in json.dumps(records)
    assert _access(records)[0]["status_code"] == 500


async def test_the_request_id_reaches_the_lines_logged_inside_the_handler(
    json_lines: Records,
) -> None:
    response = await _get(_app(), "/api/inside")

    inside = next(record for record in json_lines() if record["message"] == "inside the handler")
    assert inside["request_id"] == response.headers["x-request-id"]


async def test_a_streaming_response_keeps_the_context(json_lines: Records) -> None:
    """A BaseHTTPMiddleware would have run the app in a task of its own and
    lost the context between the middleware and the generator."""
    response = await _get(_app(), "/api/stream")

    seen = [record["seen"] for record in json_lines() if record["logger"] == "test.stream"]
    assert seen == [response.headers["x-request-id"]] * 3


async def test_the_context_is_cleared_after_the_request(json_lines: Records) -> None:
    await _get(_app(), "/api/ping")

    assert context.current() == {}


async def test_request_identity_masks_the_same_way_for_an_applications_own_handler() -> None:
    """The refusal line and the access line can never mask differently."""
    scope = {
        "type": "http",
        "method": "GET",
        "path": f"/api/people/{CITIZEN_IDENTIFIER}/certificate",
        "path_params": {"codice_fiscale": CITIZEN_IDENTIFIER},
    }

    assert request_identity(scope) == {
        "method": "GET",
        "route": "/api/people/{codice_fiscale}/certificate",
        "path": "/api/people/*/certificate",
    }


def test_installing_on_something_that_is_not_an_asgi_application_is_refused() -> None:
    with pytest.raises(TypeError, match="add_middleware"):
        install_request_logging(object())

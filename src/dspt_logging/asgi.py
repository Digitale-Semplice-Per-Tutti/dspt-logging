"""One access line per request, and the request's identity for every other line.

Pure ASGI on purpose, and not a Starlette ``BaseHTTPMiddleware``: that class
buffers streaming responses (an assistant streams SSE for minutes) and runs the
application in a task of its own, which loses the context variables between the
middleware and the handler. Nothing here imports Starlette or FastAPI, so the
package stays dependency-free and the same middleware works under any ASGI
server.

What the access line says: the request id (the caller's ``X-Request-ID`` when it
sent a usable one, otherwise ours, and always echoed back on the response), the
method, the matched ``route`` template, the ``path`` with identifying segments
masked, the status, the duration, the caller's address and its user agent. The
query string is never logged: it carries tokens and codici fiscali, and a log
line is kept for two years.

Probes are logged only when they fail: kubelet and Prometheus hit them every
few seconds and a 200 says nothing, a 503 says everything. Readiness is silent
even then, because the readiness route keeps a rate-limited record of its own —
an access line per probe undid that limit exactly when it mattered, leaving 51
access lines against 4 readiness events during one four-minute outage.

An exception the application lets through is logged here as ``api.unhandled``
with its type and stack (never its value), then re-raised so the server answers
500 in its usual way.
"""

from __future__ import annotations

import ipaddress
import logging
import secrets
import time
from collections.abc import Iterable, Sequence
from typing import Any, Final

from dspt_logging import context
from dspt_logging.events import API_UNHANDLED, HTTP_ACCESS, log_event

logger = logging.getLogger("dspt_logging.access")

REQUEST_ID_HEADER: Final[bytes] = b"x-request-id"

#: Liveness, readiness and the Prometheus scrape, under the two prefixes the
#: fleet's applications use. A 2xx on one of these leaves no line.
DEFAULT_PROBE_PATHS: Final[tuple[str, ...]] = (
    "/health",
    "/api/health",
    "/ready",
    "/api/ready",
    "/metrics",
)

#: The probes that keep a record of their own, and so need no access line at
#: all — not even when they fail.
SELF_LOGGING_PROBE_PATHS: Final[tuple[str, ...]] = ("/ready", "/api/ready")

#: Path parameters whose value must never reach a log line: a secret (an
#: activation token) or a citizen's identifier (a codice fiscale, which routes
#: like ``/requests/by-cf/{cf}`` carry in the open). Masked in ``path``, while
#: ``route`` keeps the template so the line stays aggregable. Matched by
#: *name*, wherever the route is declared: it is a naming convention, not
#: knowledge of any one application's routes.
DEFAULT_MASKED_PARAMS: Final[tuple[str, ...]] = ("token", "cf", "codice_fiscale")

USER_AGENT_MAX_LENGTH: Final[int] = 200

#: Where the middleware leaves its masked-parameter names, so that
#: :func:`request_identity` masks a refusal line exactly as the access line did.
_MASKED_PARAMS_SCOPE_KEY: Final[str] = "dspt_logging.masked_params"


def _substitute(path: str, path_params: dict[str, Any], replacement: Any) -> str:
    """The raw path with each path parameter's segment replaced.

    Whole segments only, so a value that happens to be a substring of another
    segment is left alone.
    """
    by_value = {str(value): name for name, value in path_params.items() if value}
    segments = path.split("/")
    return "/".join(
        replacement(by_value[segment]) if segment in by_value else segment for segment in segments
    )


def route_template(path: str, path_params: dict[str, Any]) -> str:
    """``/api/tenants/{slug}/enter`` for ``/api/tenants/example/enter``.

    Computed from the request rather than read off ``scope["route"]``: with
    nested routers that object carries the sub-router's path (``/auth/me``),
    not the one the caller used.
    """
    return _substitute(path, path_params, lambda name: "{" + name + "}")


def masked_path(
    path: str,
    path_params: dict[str, Any],
    masked_params: Iterable[str] = DEFAULT_MASKED_PARAMS,
) -> str:
    """The raw path with the value of every masked path parameter replaced by ``*``."""
    names = frozenset(masked_params)
    return _substitute(
        path,
        {name: value for name, value in path_params.items() if name in names},
        lambda _name: "*",
    )


def request_identity(scope: Any) -> dict[str, Any]:
    """``method``, ``route`` and the masked ``path`` of the request being served.

    Available once routing has run, which is before any response and before any
    handler exception. An application's own error handlers use it too, so its
    refusal line and this middleware's access line can never mask differently.
    """
    path = scope.get("path", "")
    path_params = scope.get("path_params") or {}
    masked_params = scope.get(_MASKED_PARAMS_SCOPE_KEY, DEFAULT_MASKED_PARAMS)
    return {
        "method": scope.get("method"),
        "route": route_template(path, path_params),
        "path": masked_path(path, path_params, masked_params),
    }


def _header(scope: Any, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key.lower() == name:
            decoded: str = value.decode("latin-1")
            return decoded.strip()
    return None


def _valid_ip(value: str | None) -> str | None:
    """``value`` only if it parses as an address.

    Caller-controlled headers land in a searchable field, and an audit row in
    the applications goes to a Postgres ``inet`` column that refuses anything
    else.
    """
    if not value:
        return None
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return None
    return value


def client_ip(scope: Any) -> str | None:
    """The caller's address: ``X-Forwarded-For``, then ``X-Real-IP``, then the socket.

    The socket is never the citizen: behind a CDN and an ingress it is a load
    balancer or another pod of ours. It is kept as the last resort because for
    a forensic record a wrong-but-present address beats a null — anything that
    makes a *decision* from the address (a rate limiter) must use the forwarded
    value only.
    """
    forwarded = _header(scope, b"x-forwarded-for")
    if forwarded:
        candidate = _valid_ip(forwarded.split(",")[0].strip())
        if candidate:
            return candidate
    candidate = _valid_ip(_header(scope, b"x-real-ip"))
    if candidate:
        return candidate
    peer = scope.get("client")
    return _valid_ip(peer[0]) if peer else None


def _incoming_request_id(scope: Any) -> str | None:
    candidate = _header(scope, REQUEST_ID_HEADER)
    # Bounded and printable: a header is caller-controlled and lands in a
    # searchable field that joins a frontend line to a backend one.
    if candidate and 1 <= len(candidate) <= 64 and candidate.isprintable():
        return candidate
    return None


class RequestContextMiddleware:
    """Bind the request id, emit the access line, name the unhandled exception."""

    def __init__(
        self,
        app: Any,
        *,
        probe_paths: Sequence[str] = DEFAULT_PROBE_PATHS,
        masked_params: Sequence[str] = DEFAULT_MASKED_PARAMS,
        quiet_404_outside: Sequence[str] | None = None,
        silent_user_agents: Sequence[str] = (),
    ) -> None:
        self.app = app
        self.probe_paths = tuple(probe_paths)
        self.masked_params = tuple(masked_params)
        self.quiet_404_outside = tuple(quiet_404_outside) if quiet_404_outside else None
        # An application that probes one of its own public routes (a webhook
        # handshake, every couple of minutes, per tenant) cannot list the
        # route as a probe: real callers use it too. It names itself in the
        # user agent instead, and those lines are silent whatever the answer.
        self.silent_user_agents = tuple(silent_user_agents)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        scope[_MASKED_PARAMS_SCOPE_KEY] = self.masked_params
        request_id = _incoming_request_id(scope) or secrets.token_urlsafe(9)
        caller_ip = client_ip(scope)
        user_agent = _header(scope, b"user-agent")
        tokens = context.bind(request_id=request_id)
        started = time.perf_counter()
        status: dict[str, int] = {}

        async def send_with_id(message: Any) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                headers = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() != REQUEST_ID_HEADER
                ]
                headers.append((REQUEST_ID_HEADER, request_id.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            try:
                await self.app(scope, receive, send_with_id)
            except Exception:
                status.setdefault("code", 500)
                log_event(logger, API_UNHANDLED, exc_info=True, **request_identity(scope))
                raise
            finally:
                self._log_access(scope, status.get("code"), started, caller_ip, user_agent)
        finally:
            context.unbind(tokens)

    def _log_access(
        self,
        scope: Any,
        status_code: int | None,
        started: float,
        caller_ip: str | None,
        user_agent: str | None,
    ) -> None:
        raw_path = scope.get("path", "")
        # A probe that writes its own line needs no second one, whatever it
        # answered.
        if raw_path.startswith(SELF_LOGGING_PROBE_PATHS):
            return
        if status_code is not None and 200 <= status_code < 300 and self._is_probe(raw_path):
            return
        if (
            user_agent
            and self.silent_user_agents
            and user_agent.startswith(self.silent_user_agents)
        ):
            return

        fields: dict[str, Any] = dict(request_identity(scope))
        if caller_ip is not None:
            fields["client_ip"] = caller_ip
        if user_agent:
            fields["user_agent"] = user_agent[:USER_AGENT_MAX_LENGTH]
        log_event(
            logger,
            HTTP_ACCESS,
            level=self._level(raw_path, status_code),
            status_code=status_code,
            duration_ms=int((time.perf_counter() - started) * 1000),
            **fields,
        )

    def _is_probe(self, path: str) -> bool:
        return path.startswith(self.probe_paths)

    def _level(self, path: str, status_code: int | None) -> int:
        """INFO, unless this is a 404 on a path no router of the application owns.

        Those are vulnerability scanners (``/xmlrpc.php``, ``/wp-includes/...``:
        hundreds of lines a day across the fleet). An application passes the
        prefixes it actually serves; a 404 *under* them is a missing asset or a
        wrong route and stays INFO, because that one is a defect of ours.
        """
        if (
            self.quiet_404_outside is not None
            and status_code == 404
            and not path.startswith(self.quiet_404_outside)
        ):
            return logging.DEBUG
        return logging.INFO


def install_request_logging(
    app: Any,
    *,
    probe_paths: Sequence[str] = DEFAULT_PROBE_PATHS,
    masked_params: Sequence[str] = DEFAULT_MASKED_PARAMS,
    quiet_404_outside: Sequence[str] | None = None,
    silent_user_agents: Sequence[str] = (),
) -> None:
    """Mount :class:`RequestContextMiddleware` on an ASGI application.

    Duck-typed against Starlette's ``add_middleware`` rather than typed against
    it, so this package keeps no web framework as a dependency. The middleware
    logs the unhandled exception and re-raises it, leaving the server's own
    error middleware to answer 500 — there is nothing else to register.
    """
    add_middleware = getattr(app, "add_middleware", None)
    if add_middleware is None:
        raise TypeError(
            "install_request_logging() needs an application with add_middleware() "
            "(FastAPI or Starlette); wrap it with RequestContextMiddleware yourself otherwise"
        )
    add_middleware(
        RequestContextMiddleware,
        probe_paths=tuple(probe_paths),
        masked_params=tuple(masked_params),
        quiet_404_outside=tuple(quiet_404_outside) if quiet_404_outside else None,
        silent_user_agents=tuple(silent_user_agents),
    )

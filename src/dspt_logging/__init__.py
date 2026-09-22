"""The shared logging contract of the DSPT backends.

Three lines in an application's entrypoint::

    from dspt_logging import configure_logging, install_request_logging

    configure_logging("example-backend", tenant=TENANT_SLUG)
    install_request_logging(app, quiet_404_outside=("/api", "/static"))

and every line the process writes is one flat JSON object with the same keys,
every request carries an id from the edge to the log platform, and the
catalogue check refuses a log line nobody named.
"""

from __future__ import annotations

from dspt_logging.asgi import (
    RequestContextMiddleware,
    install_request_logging,
    request_identity,
)
from dspt_logging.config import (
    JsonFormatter,
    MergingLoggerAdapter,
    configure_logging,
    resolve_log_level,
)
from dspt_logging.context import bind, bound, current, unbind
from dspt_logging.events import (
    API_UNHANDLED,
    HTTP_ACCESS,
    LOGGING_LEVEL_INVALID,
    MIGRATION_APPLIED,
    PYTHON_WARNING,
    RESERVED_FIELDS,
    SHARED_FIELDS,
    Event,
    log_event,
)

__version__ = "0.1.2"

__all__ = [
    "API_UNHANDLED",
    "HTTP_ACCESS",
    "LOGGING_LEVEL_INVALID",
    "MIGRATION_APPLIED",
    "PYTHON_WARNING",
    "RESERVED_FIELDS",
    "SHARED_FIELDS",
    "Event",
    "JsonFormatter",
    "MergingLoggerAdapter",
    "RequestContextMiddleware",
    "__version__",
    "bind",
    "bound",
    "configure_logging",
    "current",
    "install_request_logging",
    "log_event",
    "request_identity",
    "resolve_log_level",
    "unbind",
]

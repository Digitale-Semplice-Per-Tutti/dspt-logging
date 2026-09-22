"""A minimal application wired the way every DSPT backend is.

Three lines of setup, one catalogue, and no logging code of its own. CI runs
the catalogue check over this directory, so a regression in the checker is
caught here rather than in an application.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from dspt_logging import bound, configure_logging, install_request_logging, log_event
from examples.events import APP_STARTUP_COMPLETED, APPOINTMENT_BOOKED, APPOINTMENT_REFUSED

logger = logging.getLogger(__name__)

SERVICE = "example-backend"
VERSION = "0.1.0"


def create_app() -> FastAPI:
    # One deployment, one municipality: the tenant slug is on every line. A
    # deployment that serves several passes tenant=None and binds the slug on
    # the context where it learns it.
    configure_logging(SERVICE, tenant="tenant-a")

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        log_event(logger, APP_STARTUP_COMPLETED, version=VERSION)
        yield

    app = FastAPI(lifespan=lifespan)
    # `quiet_404_outside` is the list of prefixes this application actually
    # serves: a 404 anywhere else is a vulnerability scanner and goes to DEBUG.
    install_request_logging(app, quiet_404_outside=("/api", "/static"))

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/appointments/{appointment_id}/confirm")
    async def confirm(appointment_id: str, channel: str = "web") -> dict[str, str]:
        # The user the request authenticated as, and the thread it belongs to,
        # would be bound by the application's auth dependency; here the block
        # shows what binding looks like.
        with bound(user_id=42):
            if channel not in {"web", "whatsapp"}:
                log_event(
                    logger,
                    APPOINTMENT_REFUSED,
                    appointment_id=appointment_id,
                    channel=channel,
                    reason="unknown channel",
                )
                return {"status": "refused"}
            log_event(
                logger,
                APPOINTMENT_BOOKED,
                appointment_id=appointment_id,
                channel=channel,
                duration_ms=12,
            )
            return {"status": "booked"}

    return app

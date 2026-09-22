"""The example application's event catalogue.

One file per package, and the only place an event name is written. The package
owns the four platform events (``http.access``, ``api.unhandled``,
``python.warning``, ``migration.applied``); everything an application says
about its own work lives in a file like this one.
"""

from __future__ import annotations

import logging
from typing import Final

from dspt_logging import Event

#: The fields every appointment line carries. A module-level constant, because
#: the checker can read one and cannot read a helper's return value.
_APPOINTMENT: Final = frozenset({"appointment_id", "channel"})

APP_STARTUP_COMPLETED: Final = Event(
    name="app.startup.completed",
    message="Example API ready",
    required=frozenset({"version"}),
)

APPOINTMENT_BOOKED: Final = Event(
    name="appointment.booking.completed",
    message="Appointment booked",
    required=_APPOINTMENT,
    optional=frozenset({"duration_ms"}),
)

APPOINTMENT_REFUSED: Final = Event(
    name="appointment.booking.refused",
    # A refusal the system handled: it did not work, and the system held.
    message="Appointment refused",
    level=logging.WARNING,
    required=_APPOINTMENT | frozenset({"reason"}),
)

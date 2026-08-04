"""Diagnostics support for the Budbee parcel tracker integration."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import BudbeeConfigEntry

# Diagnostics are pasted into public issues, so redact anything that
# identifies a person, an address or a specific parcel. Over-redacting is
# cheap; under-redacting leaks a user's home address into a GitHub thread.
#
# Budbee's payload is unusually rich: besides the recipient block it carries
# the locker's exact coordinates, the depot, and — on a door delivery — the
# door code and the collection PIN. The last group is non-negotiable: those are
# physical access codes, and a diagnostics file with one in it is a key posted
# in public. ``async_redact_data`` matches on key names at any depth, so nested
# blocks are covered by naming the block.
TO_REDACT = {
    # canonical fields we publish ourselves
    "tracking_code",
    "barcode",
    "sender",
    "receiver",
    "url",
    "pickup_point",
    # the parcel's identity on the wire — the token *is* the credential
    "token",
    "trackingId",
    "authCode",
    # physical access codes
    "identification",
    "identificationAtLocker",
    "deliveryPinCode",
    "entryAccessCode",
    # the recipient, and every block that spells out where they live
    "consumer",
    "consumerAddress",
    "address",
    "lockerAddress",
    "coordinate",
    "street",
    "street2",
    "postalCode",
    "city",
    "directions",
    "email",
    "phoneNumber",
    "name",
    # the driver, and the merchant's support contacts
    "driver",
    "supportEmail",
    "supportPhone",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: BudbeeConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for the Budbee config entry."""
    coordinator = entry.runtime_data.coordinator

    return {
        "entry_options": async_redact_data(dict(entry.options), TO_REDACT),
        "counts": {
            "incoming_active": len(coordinator.data or []),
            "delivered": len(coordinator.delivered or []),
            "outgoing_active": len(coordinator.outgoing or []),
            "outgoing_delivered": len(coordinator.delivered_outgoing or []),
        },
        "incoming": async_redact_data(coordinator.data or [], TO_REDACT),
        "delivered": async_redact_data(coordinator.delivered or [], TO_REDACT),
        "outgoing": async_redact_data(coordinator.outgoing or [], TO_REDACT),
        "outgoing_delivered": async_redact_data(
            coordinator.delivered_outgoing or [], TO_REDACT
        ),
    }

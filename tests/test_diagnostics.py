"""Tests for Budbee diagnostics."""
from unittest.mock import MagicMock

from custom_components.budbee.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.budbee.parcels import normalize_parcel

from .payloads import ACTIVE_CODE, box_sample


async def test_diagnostics_redacts_and_counts(hass):
    """Diagnostics get pasted into public issues — nothing identifying may survive."""
    entry = MagicMock()
    entry.options = {"parcels": [{"tracking_code": ACTIVE_CODE}]}
    entry.runtime_data.coordinator.data = [normalize_parcel(box_sample())]
    entry.runtime_data.coordinator.delivered = []
    entry.runtime_data.coordinator.outgoing = []
    entry.runtime_data.coordinator.delivered_outgoing = []

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["counts"] == {
        "incoming_active": 1,
        "delivered": 0,
        "outgoing_active": 0,
        "outgoing_delivered": 0,
    }
    redacted = "**REDACTED**"
    parcel = result["incoming"][0]
    # tracking codes and payload PII are redacted, at every nesting level
    assert result["entry_options"]["parcels"][0]["tracking_code"] == redacted
    assert parcel["barcode"] == redacted
    assert parcel["url"] == redacted
    assert parcel["pickup_point"] == redacted
    assert parcel["raw"]["token"] == redacted
    assert parcel["raw"]["consumer"] == redacted
    assert parcel["raw"]["consumerAddress"] == redacted
    # the locker gives away where the user collects their parcels
    assert parcel["raw"]["lockerAddress"] == redacted
    assert parcel["raw"]["lockerAttributes"]["address"] == redacted
    # non-identifying fields survive, or the diagnostics would be useless
    assert parcel["status"] == "registered"
    assert parcel["raw_status"] == "NotStarted"
    assert parcel["raw"]["meta"]["type"] == "BOX"


async def test_diagnostics_redacts_physical_access_codes(hass):
    """A door code in a public issue is a key posted in public."""
    entry = MagicMock()
    entry.options = {}
    raw = box_sample()
    raw["lockerAttributes"]["entryAccessCode"] = "1234"
    raw["identificationAtLocker"] = {"code": "5678"}
    raw["deliveryPinCode"] = "9012"
    raw["identification"] = {"code": "3456", "needsVerification": True}
    entry.runtime_data.coordinator.data = [normalize_parcel(raw)]
    entry.runtime_data.coordinator.delivered = []
    entry.runtime_data.coordinator.outgoing = []
    entry.runtime_data.coordinator.delivered_outgoing = []

    result = await async_get_config_entry_diagnostics(hass, entry)

    payload = result["incoming"][0]["raw"]
    redacted = "**REDACTED**"
    assert payload["lockerAttributes"]["entryAccessCode"] == redacted
    assert payload["identificationAtLocker"] == redacted
    assert payload["deliveryPinCode"] == redacted
    assert payload["identification"] == redacted

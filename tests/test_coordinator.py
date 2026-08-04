"""Tests for the Budbee coordinator: fetching, caching, direction and events.

The parcel mapping itself is covered by ``test_parcels.py``.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.budbee.api import BudbeeApiError
from custom_components.budbee.const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_PARCELS,
    CONF_TRACKING_CODE,
    DOMAIN,
    ParcelStatus,
)
from custom_components.budbee.coordinator import BudbeeCoordinator

from .payloads import (
    ACTIVE_CODE,
    DELIVERED_CODE,
    collected_sample,
    delivery_sample,
    outgoing_sample,
)

OTHER_CODE = "BUDBEE888888"


def _entry_with(parcels: list[dict]) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        # Keep-most-recent-100 so the delivered-retention filter never trims
        # the (old, fixed-date) sample parcels these tests assert on.
        options={
            CONF_PARCELS: parcels,
            CONF_DELIVERED_FILTER_TYPE: "parcels",
            CONF_DELIVERED_FILTER_AMOUNT: 100,
        },
        unique_id=DOMAIN,
    )


def _active(code: str = ACTIVE_CODE) -> dict:
    """A door delivery on the last mile — active, with an ETA window."""
    return delivery_sample(code)


def _in_transit(code: str = ACTIVE_CODE) -> dict:
    return delivery_sample(code, state="CrossDocked")


def _delivered(code: str = DELIVERED_CODE) -> dict:
    return delivery_sample(code, state="Delivered")


# ---------------------------------------------------------------------------
# fetching
# ---------------------------------------------------------------------------


async def test_update_merges_multiple_parcels(hass):
    entry = _entry_with(
        [{CONF_TRACKING_CODE: ACTIVE_CODE}, {CONF_TRACKING_CODE: DELIVERED_CODE}]
    )
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.side_effect = lambda code: (
        _active() if code == ACTIVE_CODE else _delivered()
    )
    coordinator = BudbeeCoordinator(hass, client, entry)

    data = await coordinator._async_update_data()

    assert len(data) == 1  # one active
    assert data[0]["barcode"] == ACTIVE_CODE
    assert len(coordinator.delivered) == 1
    assert coordinator.last_success_time is not None


async def test_update_not_found_shows_pending_placeholder(hass):
    entry = _entry_with([{CONF_TRACKING_CODE: OTHER_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = None  # not found
    coordinator = BudbeeCoordinator(hass, client, entry)

    data = await coordinator._async_update_data()

    assert len(data) == 1
    assert data[0]["barcode"] == OTHER_CODE
    assert data[0]["status"] == ParcelStatus.UNKNOWN


async def test_update_keeps_cached_payload_on_error(hass):
    entry = _entry_with([{CONF_TRACKING_CODE: DELIVERED_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = _delivered()
    coordinator = BudbeeCoordinator(hass, client, entry)
    await coordinator._async_update_data()  # populates the cache

    client.async_get_parcel.side_effect = BudbeeApiError("HTTP 500")
    await coordinator._async_update_data()  # error -> cached raw reused
    assert len(coordinator.delivered) == 1


async def test_update_raises_when_every_parcel_fails(hass):
    from homeassistant.helpers.update_coordinator import UpdateFailed

    entry = _entry_with([{CONF_TRACKING_CODE: DELIVERED_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.side_effect = BudbeeApiError("HTTP 500")
    coordinator = BudbeeCoordinator(hass, client, entry)

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_update_reraises_unexpected_exceptions(hass):
    """Only API and network errors are tolerated; a bug must not be swallowed."""
    entry = _entry_with([{CONF_TRACKING_CODE: DELIVERED_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.side_effect = ValueError("boom")
    coordinator = BudbeeCoordinator(hass, client, entry)

    with pytest.raises(ValueError):
        await coordinator._async_update_data()


async def test_update_skips_items_missing_a_tracking_code(hass):
    entry = _entry_with(
        [{CONF_TRACKING_CODE: ""}, {CONF_TRACKING_CODE: DELIVERED_CODE}]
    )
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = _delivered()
    coordinator = BudbeeCoordinator(hass, client, entry)

    await coordinator._async_update_data()
    assert client.async_get_parcel.await_count == 1  # empty item never fetched


async def test_update_backfills_missing_token(hass):
    """An edge payload without a token keeps the requested code."""
    entry = _entry_with([{CONF_TRACKING_CODE: OTHER_CODE}])
    entry.add_to_hass(hass)
    sample = _active()
    del sample["token"]
    client = AsyncMock()
    client.async_get_parcel.return_value = sample
    coordinator = BudbeeCoordinator(hass, client, entry)

    data = await coordinator._async_update_data()
    assert data[0]["barcode"] == OTHER_CODE


async def test_update_prunes_cache_for_untracked_parcels(hass):
    entry = _entry_with([{CONF_TRACKING_CODE: DELIVERED_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.forget = MagicMock()  # sync on the real client
    client.async_get_parcel.return_value = _delivered()
    coordinator = BudbeeCoordinator(hass, client, entry)
    coordinator._raw_cache["GONE"] = {"token": "GONE"}

    await coordinator._async_update_data()

    assert "GONE" not in coordinator._raw_cache
    assert DELIVERED_CODE in coordinator._raw_cache
    # the client's routing cache is dropped too, so a re-added code is routed
    # from scratch rather than against a stale order type
    client.forget.assert_called_once_with("GONE")


async def test_update_fetches_parcels_concurrently(hass):
    """All tracked parcels go out in one gather, not one-by-one."""
    import asyncio

    entry = _entry_with(
        [{CONF_TRACKING_CODE: ACTIVE_CODE}, {CONF_TRACKING_CODE: DELIVERED_CODE}]
    )
    entry.add_to_hass(hass)
    in_flight = 0
    peak = 0

    async def _slow_fetch(code):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0)
        in_flight -= 1
        return _active(code)

    client = AsyncMock()
    client.async_get_parcel.side_effect = _slow_fetch
    coordinator = BudbeeCoordinator(hass, client, entry)

    await coordinator._async_update_data()
    assert peak == 2


async def test_cache_only_poll_does_not_stamp_last_success(hass):
    """A poll served entirely from cache must not look like a success."""
    entry = _entry_with([{CONF_TRACKING_CODE: DELIVERED_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = _delivered()
    coordinator = BudbeeCoordinator(hass, client, entry)
    await coordinator._async_update_data()
    stamp = coordinator.last_success_time
    assert stamp is not None

    client.async_get_parcel.side_effect = BudbeeApiError("HTTP 500")
    await coordinator._async_update_data()  # served from cache
    assert coordinator.last_success_time == stamp


# ---------------------------------------------------------------------------
# direction
# ---------------------------------------------------------------------------


async def test_outgoing_shipment_lands_in_the_outgoing_list(hass, caplog):
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = outgoing_sample()
    coordinator = BudbeeCoordinator(hass, client, entry)

    data = await coordinator._async_update_data()

    assert data == []  # nothing incoming
    assert [p["barcode"] for p in coordinator.outgoing] == [ACTIVE_CODE]
    # unverified against a real return shipment, so it asks for a report — once
    assert caplog.text.count("outgoing") >= 1
    caplog.clear()
    await coordinator._async_update_data()
    assert "outgoing" not in caplog.text


async def test_collected_outgoing_shipment_lands_in_outgoing_delivered(hass):
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    sample = outgoing_sample()
    sample["status"] = "PickedUp"
    client = AsyncMock()
    client.async_get_parcel.return_value = sample
    coordinator = BudbeeCoordinator(hass, client, entry)

    await coordinator._async_update_data()

    assert coordinator.outgoing == []
    assert [p["barcode"] for p in coordinator.delivered_outgoing] == [ACTIVE_CODE]


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------


async def test_first_refresh_fires_nothing(hass):
    """Otherwise every restart floods the user with "registered" events."""
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = _active()
    coordinator = BudbeeCoordinator(hass, client, entry)

    fired = []
    for suffix in (
        "parcel_registered",
        "parcel_status_changed",
        "parcel_delivered",
        "parcel_delivery_time_changed",
        "outgoing_parcel_status_changed",
        "outgoing_parcel_delivered",
    ):
        hass.bus.async_listen(f"{DOMAIN}_{suffix}", lambda e: fired.append(e))

    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert fired == []


async def test_event_carries_device_id(hass):
    from homeassistant.helpers import device_registry as dr

    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
    )
    client = AsyncMock()
    coordinator = BudbeeCoordinator(hass, client, entry)

    events = []
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_status_changed", lambda e: events.append(e)
    )

    client.async_get_parcel.return_value = _in_transit()
    await coordinator._async_update_data()
    client.async_get_parcel.return_value = _active()
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert events[0].data["device_id"] == device.id


async def test_fires_status_changed_event(hass):
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    coordinator = BudbeeCoordinator(hass, client, entry)

    events = []
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_status_changed", lambda e: events.append(e)
    )

    client.async_get_parcel.return_value = _in_transit()
    await coordinator._async_update_data()  # first refresh: suppressed

    client.async_get_parcel.return_value = _active()  # out for delivery
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["old_status"] == ParcelStatus.IN_TRANSIT
    assert events[0].data["new_status"] == ParcelStatus.OUT_FOR_DELIVERY


async def test_locker_arrival_is_a_status_change_not_a_delivery(hass):
    """Budbee's ``Delivered`` on a locker order is not a hand-over."""
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    coordinator = BudbeeCoordinator(hass, client, entry)

    delivered = []
    changed = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_delivered", lambda e: delivered.append(e))
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_status_changed", lambda e: changed.append(e)
    )

    client.async_get_parcel.return_value = collected_sample(ACTIVE_CODE)
    client.async_get_parcel.return_value["status"] = "Collected"
    await coordinator._async_update_data()  # first refresh: suppressed

    waiting = collected_sample(ACTIVE_CODE)
    waiting["status"] = "Delivered"  # in the box
    client.async_get_parcel.return_value = waiting
    await coordinator._async_update_data()

    picked_up = collected_sample(ACTIVE_CODE)  # PickedUp
    client.async_get_parcel.return_value = picked_up
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert [e.data["new_status"] for e in changed] == [ParcelStatus.AT_PICKUP_POINT]
    assert len(delivered) == 1


async def test_delivery_fires_delivered_event_and_not_status_changed(hass):
    """The hop to delivered fires exactly one, dedicated event."""
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    coordinator = BudbeeCoordinator(hass, client, entry)

    delivered = []
    changed = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_delivered", lambda e: delivered.append(e))
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_status_changed", lambda e: changed.append(e)
    )

    client.async_get_parcel.return_value = _active(ACTIVE_CODE)
    await coordinator._async_update_data()
    client.async_get_parcel.return_value = _delivered(ACTIVE_CODE)
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert changed == []
    assert len(delivered) == 1
    assert delivered[0].data["barcode"] == ACTIVE_CODE
    assert delivered[0].data["status"] == ParcelStatus.DELIVERED


async def test_no_events_for_parcel_first_seen_delivered(hass):
    """A parcel already delivered when first tracked fires nothing at all."""
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.side_effect = lambda code: (
        _active(code) if code == ACTIVE_CODE else _delivered(code)
    )
    coordinator = BudbeeCoordinator(hass, client, entry)

    fired = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_registered", lambda e: fired.append(e))
    hass.bus.async_listen(f"{DOMAIN}_parcel_delivered", lambda e: fired.append(e))

    await coordinator._async_update_data()  # first refresh seeds the state

    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            CONF_PARCELS: [
                {CONF_TRACKING_CODE: ACTIVE_CODE},
                {CONF_TRACKING_CODE: DELIVERED_CODE},
            ],
        },
    )
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert fired == []


async def test_fires_registered_event_for_new_parcel(hass):
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = _active(ACTIVE_CODE)
    coordinator = BudbeeCoordinator(hass, client, entry)

    events = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_registered", lambda e: events.append(e))

    await coordinator._async_update_data()  # first refresh: suppressed

    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            CONF_PARCELS: [
                {CONF_TRACKING_CODE: ACTIVE_CODE},
                {CONF_TRACKING_CODE: OTHER_CODE},
            ],
        },
    )
    client.async_get_parcel.side_effect = lambda code: _active(code)
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["barcode"] == OTHER_CODE


async def test_fires_delivery_time_changed_event(hass):
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    coordinator = BudbeeCoordinator(hass, client, entry)

    events = []
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_delivery_time_changed", lambda e: events.append(e)
    )

    client.async_get_parcel.return_value = _active()
    await coordinator._async_update_data()  # first refresh: suppressed

    moved = _active()
    moved["consignment"]["start"] = "2026-04-29T16:00:00Z"
    moved["consignment"]["stop"] = "2026-04-29T18:00:00Z"
    client.async_get_parcel.return_value = moved
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["old_planned_from"] == "2026-04-29T13:00:00Z"
    assert events[0].data["new_planned_from"] == "2026-04-29T16:00:00Z"


async def test_losing_the_eta_is_silent(hass):
    """value -> null just means the carrier lost the window; not worth an alert."""
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    coordinator = BudbeeCoordinator(hass, client, entry)

    events = []
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_delivery_time_changed", lambda e: events.append(e)
    )

    client.async_get_parcel.return_value = _active()
    await coordinator._async_update_data()

    dropped = _active()
    dropped["consignment"]["start"] = None
    dropped["consignment"]["stop"] = None
    client.async_get_parcel.return_value = dropped
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert events == []


async def test_outgoing_status_change_fires_the_outgoing_event(hass):
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    coordinator = BudbeeCoordinator(hass, client, entry)

    changed = []
    delivered = []
    hass.bus.async_listen(
        f"{DOMAIN}_outgoing_parcel_status_changed", lambda e: changed.append(e)
    )
    hass.bus.async_listen(
        f"{DOMAIN}_outgoing_parcel_delivered", lambda e: delivered.append(e)
    )
    incoming = []
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_status_changed", lambda e: incoming.append(e)
    )

    sample = outgoing_sample()
    client.async_get_parcel.return_value = sample
    await coordinator._async_update_data()  # first refresh: suppressed

    dropped_off = outgoing_sample()
    dropped_off["status"] = "DroppedOff"
    client.async_get_parcel.return_value = dropped_off
    await coordinator._async_update_data()

    picked_up = outgoing_sample()
    picked_up["status"] = "PickedUp"
    client.async_get_parcel.return_value = picked_up
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert [e.data["new_status"] for e in changed] == [ParcelStatus.AT_PICKUP_POINT]
    assert len(delivered) == 1
    assert incoming == []


async def test_outgoing_parcel_first_seen_fires_nothing(hass):
    """No ``registered`` event for outgoing — same as the rest of the suite."""
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = outgoing_sample()
    coordinator = BudbeeCoordinator(hass, client, entry)

    fired = []
    for suffix in ("outgoing_parcel_status_changed", "outgoing_parcel_delivered"):
        hass.bus.async_listen(f"{DOMAIN}_{suffix}", lambda e: fired.append(e))

    await coordinator._async_update_data()

    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            CONF_PARCELS: [
                {CONF_TRACKING_CODE: ACTIVE_CODE},
                {CONF_TRACKING_CODE: OTHER_CODE},
            ],
        },
    )
    client.async_get_parcel.side_effect = lambda code: outgoing_sample(code)
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert fired == []

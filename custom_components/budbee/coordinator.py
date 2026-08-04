"""Coordinator for the Budbee parcel tracker integration.

Fetching and event firing only — the parcel mapping lives in :mod:`.parcels`.
That split is what lets the account-based variant swap this file out without
duplicating the mapping code.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import BudbeeApiClient, BudbeeApiError
from .const import (
    CONF_PARCELS,
    CONF_REFRESH_INTERVAL,
    CONF_TRACKING_CODE,
    DEFAULT_REFRESH_INTERVAL,
    DOMAIN,
    NEW_ISSUE_URL,
    ParcelStatus,
)
from .parcels import (
    apply_delivered_filter,
    is_outgoing,
    normalize_parcel,
    sort_parcels_by_ts,
)

_LOGGER = logging.getLogger(__name__)


def _refresh_interval(entry: ConfigEntry) -> timedelta:
    """Return the configured refresh interval as a ``timedelta``."""
    minutes = int(entry.options.get(CONF_REFRESH_INTERVAL, DEFAULT_REFRESH_INTERVAL))
    return timedelta(minutes=minutes)


class BudbeeCoordinator(DataUpdateCoordinator[list[dict]]):
    """Polls each tracked parcel and publishes the canonical parcel lists.

    This carrier has no account or parcel feed, so the tracked parcels are the
    tracking codes the user entered (stored in the entry options). Each is
    fetched individually and merged into one list; ``coordinator.data`` is the
    active incoming parcels, ``self.delivered`` the collected ones, and
    ``self.outgoing`` / ``self.delivered_outgoing`` the same split for parcels
    the user is *sending* (a return or an on-demand pickup).
    """

    def __init__(
        self,
        hass: HomeAssistant,
        client: BudbeeApiClient,
        entry: ConfigEntry,
    ) -> None:
        """Initialise the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            # Passing config_entry makes self.config_entry available on the
            # base class, which every helper below relies on.
            config_entry=entry,
            name=DOMAIN,
            update_interval=_refresh_interval(entry),
        )
        self._client = client
        self.delivered: list[dict] = []
        self.outgoing: list[dict] = []
        self.delivered_outgoing: list[dict] = []
        # tracking_code -> last successful raw payload, so a transient fetch
        # failure or a not-found blip keeps the parcel visible instead of
        # dropping its sensor. Lives for the integration's lifetime (resets on
        # restart).
        self._raw_cache: dict[str, dict] = {}
        # barcode -> last seen ParcelStatus, per direction, and the last seen
        # (planned_from, planned_to). ``None`` on the first refresh so events
        # are suppressed for parcels that already existed when the integration
        # started — otherwise every restart would flood users with
        # "registered" notifications.
        self._known_state: dict[str, ParcelStatus] | None = None
        self._known_outgoing_state: dict[str, ParcelStatus] | None = None
        self._known_delivery_times: (
            dict[str, tuple[str | None, str | None]] | None
        ) = None
        # Cached device id, attached to every fired event so device-trigger
        # automations can filter to this device.
        self._cached_device_id: str | None = None
        # Timestamp of the last successful poll (diagnostic sensor).
        self.last_success_time: datetime | None = None
        # Outgoing shipments are mapped from signals no live parcel has shown
        # us yet, so the first one to appear is worth a report.
        self._outgoing_reported = False

    def _device_id(self) -> str | None:
        """Resolve (and cache) this entry's device id for event payloads."""
        if self._cached_device_id is not None:
            return self._cached_device_id
        registry = dr.async_get(self.hass)
        device = next(
            iter(
                dr.async_entries_for_config_entry(registry, self.config_entry.entry_id)
            ),
            None,
        )
        if device is not None:
            self._cached_device_id = device.id
        return self._cached_device_id

    def _tracked(self) -> list[str]:
        """Return the configured tracking codes."""
        return [
            item[CONF_TRACKING_CODE]
            for item in self.config_entry.options.get(CONF_PARCELS, [])
            if item.get(CONF_TRACKING_CODE)
        ]

    async def _async_update_data(self) -> list[dict]:
        """Fetch every tracked parcel and split by direction and delivery."""
        codes = self._tracked()

        # Drop cache entries for parcels the user no longer follows, so the
        # cache stays bounded — and let the client forget their routing too, so
        # a re-added code is routed afresh.
        tracked_codes = set(codes)
        for code in list(self._raw_cache):
            if code not in tracked_codes:
                del self._raw_cache[code]
                self._client.forget(code)

        results = await asyncio.gather(
            *(self._client.async_get_parcel(code) for code in codes),
            return_exceptions=True,
        )

        raws: list[dict] = []
        errors = 0
        for code, result in zip(codes, results):
            if isinstance(result, BaseException):
                if not isinstance(
                    result, (BudbeeApiError, aiohttp.ClientError)
                ):
                    raise result
                errors += 1
                _LOGGER.warning("Budbee fetch failed for %s: %s", code, result)
                cached = self._raw_cache.get(code)
                if cached is not None:
                    raws.append(cached)
                continue

            if result is None:
                # Unknown code, or not handed to Budbee yet. Keep prior data if
                # we have it, otherwise show a pending placeholder so the user
                # still sees the parcel they asked us to track.
                raws.append(self._raw_cache.get(code) or {"token": code})
                continue

            # The response's own token can be missing on edge payloads; fall
            # back to the code we asked for so the sensor keeps its key.
            result.setdefault("token", code)
            self._raw_cache[code] = result
            raws.append(result)

        if codes and errors == len(codes) and not raws:
            raise UpdateFailed("Budbee unreachable for all tracked parcels")

        normalized = [(raw, normalize_parcel(raw)) for raw in raws]
        incoming = [parcel for raw, parcel in normalized if not is_outgoing(raw)]
        outgoing = [parcel for raw, parcel in normalized if is_outgoing(raw)]
        if outgoing:
            self._report_outgoing()

        self.delivered, active_in = self._split_delivered(incoming)
        self.delivered_outgoing, self.outgoing = self._split_delivered(outgoing)

        # Incoming = active + delivered, combined so the transition to
        # delivered is visible in one set.
        combined_in = active_in + self.delivered
        combined_out = self.outgoing + self.delivered_outgoing
        self._fire_change_events(combined_in)
        self._fire_outgoing_change_events(combined_out)
        self._known_state = self._status_map(combined_in)
        self._known_outgoing_state = self._status_map(combined_out)
        self._known_delivery_times = {
            parcel["barcode"]: (parcel.get("planned_from"), parcel.get("planned_to"))
            for parcel in combined_in
            if parcel.get("barcode")
        }

        # Only stamp the diagnostic timestamp when at least one fetch actually
        # succeeded (or nothing is tracked) — a poll served entirely from cache
        # must not present itself as a successful update.
        if not codes or errors < len(codes):
            self.last_success_time = datetime.now(timezone.utc)
        return active_in

    def _split_delivered(self, parcels: list[dict]) -> tuple[list[dict], list[dict]]:
        """Return (delivered, active) — delivered filtered, both sorted."""
        delivered = apply_delivered_filter(
            sort_parcels_by_ts(
                [p for p in parcels if p["delivered"]], "delivered_at", descending=True
            ),
            self.config_entry,
        )
        active = sort_parcels_by_ts(
            [p for p in parcels if not p["delivered"]], "planned_from"
        )
        return delivered, active

    @staticmethod
    def _status_map(parcels: list[dict]) -> dict[str, ParcelStatus]:
        """Map barcode -> status, for the next poll's change detection."""
        return {p["barcode"]: p["status"] for p in parcels if p.get("barcode")}

    def _report_outgoing(self) -> None:
        """Ask for a report the first time an outgoing shipment is recognised.

        The direction split reads ``consignment.type`` and the
        ``CollectedShippingLabel`` status, neither of which has been seen on a
        live Budbee shipment. One WARNING per HA session, keys only — the
        payload carries an address.
        """
        if self._outgoing_reported:
            return
        self._outgoing_reported = True
        _LOGGER.warning(
            "Budbee reported a parcel as outgoing (one you are sending) for "
            "the first time. That split has never been checked against a real "
            "return shipment, so please tell us whether it got it right — "
            "either way. If the parcel is one you are *receiving*, it is now "
            "on the wrong sensor and we want to know."
            "\n  Please report it: %s",
            NEW_ISSUE_URL,
        )

    def _fire_change_events(self, parcels: list[dict]) -> None:
        """Fire registered / status-changed / delivered / delivery-time events.

        Silent on the very first refresh — we cannot know which parcels are
        genuinely new versus already present before HA started.

        The event contract, identical across the suite:

        * every payload is the full normalised parcel plus ``device_id``;
        * the hop **to** ``delivered`` fires only ``_parcel_delivered``, never
          also ``_parcel_status_changed``;
        * a barcode first seen already-delivered fires nothing;
        * ``registered`` only fires for a new, not-yet-delivered barcode;
        * an ETA going ``value → null`` is intentionally silent — the carrier
          just lost the window, which is not worth waking someone up for.

        On a locker order ``delivered`` is ``PickedUp``, not Budbee's own
        ``Delivered`` — that one means the parcel reached the locker, and fires
        a status change to ``at_pickup_point`` instead.
        """
        if self._known_state is None:
            return

        known_times = self._known_delivery_times or {}
        device_id = self._device_id()

        for parcel in parcels:
            barcode = parcel.get("barcode")
            if not barcode:
                continue
            new_status = parcel["status"]
            if barcode not in self._known_state:
                if new_status != ParcelStatus.DELIVERED:
                    self.hass.bus.async_fire(
                        f"{DOMAIN}_parcel_registered",
                        {**parcel, "device_id": device_id},
                    )
                continue

            if self._known_state[barcode] != new_status:
                if new_status == ParcelStatus.DELIVERED:
                    self.hass.bus.async_fire(
                        f"{DOMAIN}_parcel_delivered",
                        {**parcel, "device_id": device_id},
                    )
                else:
                    self.hass.bus.async_fire(
                        f"{DOMAIN}_parcel_status_changed",
                        {
                            **parcel,
                            "device_id": device_id,
                            "old_status": self._known_state[barcode],
                            "new_status": new_status,
                        },
                    )

            old_from, old_to = known_times.get(barcode, (None, None))
            new_from = parcel.get("planned_from")
            new_to = parcel.get("planned_to")
            from_changed = new_from is not None and new_from != old_from
            to_changed = new_to is not None and new_to != old_to
            if from_changed or to_changed:
                self.hass.bus.async_fire(
                    f"{DOMAIN}_parcel_delivery_time_changed",
                    {
                        **parcel,
                        "device_id": device_id,
                        "old_planned_from": old_from,
                        "new_planned_from": new_from,
                        "old_planned_to": old_to,
                        "new_planned_to": new_to,
                    },
                )

    def _fire_outgoing_change_events(self, parcels: list[dict]) -> None:
        """Fire outgoing status-changed / delivered events.

        No ``registered`` and no delivery-time event for outgoing (deliberate,
        same as the rest of the suite). The hop to ``delivered`` fires only
        ``_outgoing_parcel_delivered``.
        """
        if self._known_outgoing_state is None:
            return
        device_id = self._device_id()
        for parcel in parcels:
            barcode = parcel.get("barcode")
            if not barcode or barcode not in self._known_outgoing_state:
                continue
            new_status = parcel["status"]
            if self._known_outgoing_state[barcode] == new_status:
                continue
            if new_status == ParcelStatus.DELIVERED:
                self.hass.bus.async_fire(
                    f"{DOMAIN}_outgoing_parcel_delivered",
                    {**parcel, "device_id": device_id},
                )
            else:
                self.hass.bus.async_fire(
                    f"{DOMAIN}_outgoing_parcel_status_changed",
                    {
                        **parcel,
                        "device_id": device_id,
                        "old_status": self._known_outgoing_state[barcode],
                        "new_status": new_status,
                    },
                )

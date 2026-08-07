"""Canonical parcel shape, status mapping and list helpers.

Everything in this module is a **pure function** — no I/O, no Home Assistant
objects beyond the config entry's options. That is deliberate: it keeps the
carrier-specific mapping apart from the coordinator (which is nearly identical
everywhere), and it makes the mapping trivially unit-testable without spinning
up HA.

Budbee needs **two** status maps and **two** payload readers, because it serves
locker orders and door deliveries from different routes with different shapes —
and because ``Delivered`` means *handed to you* on a door delivery but *sitting
in a locker* on a locker order. The order type comes from the keyless ``meta``
call the API client merges into every payload; when it is missing or new, the
payload's own shape decides.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry

from .const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    NEW_ISSUE_URL,
    ORDER_TYPE_BOX,
    ORDER_TYPE_DELIVERY,
    OUTGOING_CONSIGNMENT_TYPES,
    STATUS_COLLECTED_SHIPPING_LABEL,
    TRACKING_URL,
    ParcelStatus,
)

_LOGGER = logging.getLogger(__name__)

# Budbee masks the recipient's own details with this literal unless the caller
# presents the per-order auth code. We deliberately do not ask users for the
# e-mail that would buy that code, so the mask is the normal case and must read
# as "not exposed", not as a name.
_MASKED = "*****"

# Door deliveries — 11 values, a closed enum from Budbee's own tracking client.
_STATUS_MAP_DELIVERY: dict[str, ParcelStatus] = {
    "NotStarted": ParcelStatus.REGISTERED,
    "OnRouteCollection": ParcelStatus.IN_TRANSIT,
    "Collected": ParcelStatus.IN_TRANSIT,
    "CrossDocked": ParcelStatus.IN_TRANSIT,
    "OnRouteDelivery": ParcelStatus.OUT_FOR_DELIVERY,
    "Delivered": ParcelStatus.DELIVERED,
    "Miss": ParcelStatus.PROBLEM,
    "Backordered": ParcelStatus.PROBLEM,
    STATUS_COLLECTED_SHIPPING_LABEL: ParcelStatus.IN_TRANSIT,
    # "Returned" names arrival at a Budbee terminal, not a reversal — ordinary
    # forward progress, confirmed live. Only ``ReturnedToMerchant`` is the
    # genuine going-back-to-the-sender state.
    "ReturnedToTerminal": ParcelStatus.IN_TRANSIT,
    "ReturnedToMerchant": ParcelStatus.RETURNING,
}

# Locker orders — 10 values, same source. This carrier needs two maps because
# ``Delivered`` — and only ``Delivered`` — means *waiting in the locker for
# you*; reading the door map here would announce a delivery while the parcel
# is still in a hatch. ``DroppedOff`` reads like the same event but is not: it
# fires whenever a parcel is placed into a locker by anyone — a courier's
# hand-off for onward transit reads identically to a customer's own drop-off —
# so direction and finality are not encoded in the status string itself.
# Confirmed live as the *sender's* hand-off, hours before the parcel reached a
# terminal. ``ReturnedToTerminal`` is the same trap: "Returned" names arrival
# at a Budbee terminal, not a reversal — see ``_STATUS_MAP_DELIVERY`` above.
_STATUS_MAP_BOX: dict[str, ParcelStatus] = {
    "NotStarted": ParcelStatus.REGISTERED,
    "Pending": ParcelStatus.REGISTERED,
    "Collected": ParcelStatus.IN_TRANSIT,
    STATUS_COLLECTED_SHIPPING_LABEL: ParcelStatus.IN_TRANSIT,
    "DroppedOff": ParcelStatus.IN_TRANSIT,
    "Delivered": ParcelStatus.AT_PICKUP_POINT,
    "PickedUp": ParcelStatus.DELIVERED,
    "Undelivered": ParcelStatus.PROBLEM,
    "ReturnedToTerminal": ParcelStatus.IN_TRANSIT,
    "ReturnedToMerchant": ParcelStatus.RETURNING,
}

# Top-level keys each payload is known to carry. A key outside these is real
# news: the two payloads below were mapped from one live locker order and one
# reconstruction, so a shape change — or a field we never saw populated — is
# exactly what this integration is missing.
_KNOWN_BOX_KEYS = frozenset(
    {
        "authorized", "token", "createdAt", "timeZone", "status",
        "pickedUpFromMerchant", "deliveredAt", "latestPickupDate", "eta",
        "etaInformation", "orderLocation", "consignment", "recall", "returns",
        "merchant", "lockerIdentifier", "lockerAddress", "lockerAttributes",
        # Reported by a live locker order on 2026-08-04, alongside the
        # duplicated ``openHours`` — a locker attribute, nothing canonical.
        "lockerBrands",
        "openHours", "demand", "consumer", "consumerAddress",
        "identificationAtLocker", "rating", "campaign", "nudges",
        "canSwitchDeliveryType", "canSwitchLocker", "canExtendPickupTime",
        # merged in by the API client, not part of the carrier's own body
        "meta",
    }
)
_KNOWN_DELIVERY_KEYS = frozenset(
    {
        "token", "createdAt", "timeZone", "status", "consignment", "routedEta",
        "eta", "merchant", "consumer", "address", "driver", "lockerAttributes",
        "orderLocation", "identification", "deliveryPinCode", "settings",
        "deliverySettings", "parcelDeliveryOverview", "returns", "recall",
        "nudges", "cancellation", "latestPickupDate", "deliveryOptions",
        "externalLinks", "allowedToBookDelivery", "canSwitchDeliveryType",
        "canSwitchLocker", "canExtendPickupTime",
        "meta",
    }
)

# Budbee's own consumer brand, as reported by the keyless meta call. The
# sibling brands share this backend; seeing one here would be the first
# evidence that they are trackable through the same routes.
_OWN_BRAND = "BUDBEE"

# Everything already reported this HA session, so each finding is logged once
# rather than on every poll.
_warned: set[str] = set()


def _warn_once(key: str, message: str, *args: Any) -> None:
    """Log ``message`` as a WARNING the first time ``key`` comes up.

    WARNING and not INFO/DEBUG on purpose: Home Assistant's default log level
    hides those, and an unreported unknown is the one thing this integration
    cannot fix on its own. Everything logged here is a **key, a status or a
    type — never a value**, because these payloads carry an address and, on a
    door delivery, a door code.
    """
    if key in _warned:
        return
    _warned.add(key)
    _LOGGER.warning(message + "\n  Please report it: %s", *args, NEW_ISSUE_URL)


def _warn_unmapped_status(order_type: str, code: str) -> None:
    """Log an unmapped carrier status once, with a copy-paste issue link."""
    _warn_once(
        f"status:{order_type}:{code}",
        "Unrecognised Budbee status — help us map it."
        "\n  status=%s type=%s → reported as 'unknown'",
        code,
        order_type,
    )


def _warn_unexpected_keys(order: str, raw: dict) -> None:
    """Report payload keys this integration has never seen."""
    known = _KNOWN_BOX_KEYS if order == ORDER_TYPE_BOX else _KNOWN_DELIVERY_KEYS
    for key in sorted(set(raw) - known):
        _warn_once(
            f"key:{order}:{key}",
            "Budbee sent a payload field we do not know."
            "\n  field=%s type=%s → ignored (it may carry something useful)",
            key,
            order,
        )


def _warn_unverified_shapes(order: str, raw: dict, status: ParcelStatus) -> None:
    """Report everything about this parcel that has never been seen live.

    Budbee shipped with exactly one confirmed payload: an NL locker order that
    had not moved yet. Each check below marks a place where this integration is
    working from a reconstruction or an assumption, so the first real user to
    hit it turns it into evidence.
    """
    _warn_unexpected_keys(order, raw)

    meta = raw.get("meta")
    if not isinstance(meta, dict) or not meta.get("type"):
        _warn_once(
            "meta:missing",
            "Budbee did not route this order — the order type was guessed from "
            "the payload's shape."
            "\n  guessed_type=%s → the status vocabulary may be the wrong one",
            order,
        )
    else:
        brand = meta.get("brand")
        if brand and brand != _OWN_BRAND:
            _warn_once(
                f"brand:{brand}",
                "This Budbee parcel reports a different consumer brand. It is "
                "tracked correctly, but we would like to know it works."
                "\n  brand=%s",
                brand,
            )

    if order == ORDER_TYPE_DELIVERY:
        report_structure(raw)
        return

    if status is ParcelStatus.DELIVERED:
        _warn_once(
            "shape:box-picked-up",
            "First Budbee locker parcel collected. Please confirm the "
            "collection time on the sensor matches when you actually took it "
            "out of the locker."
            "\n  deliveredAt_present=%s",
            raw.get("deliveredAt") is not None,
        )

    if raw.get("eta") or raw.get("etaInformation"):
        _warn_once(
            "shape:box-eta",
            "A Budbee locker parcel carries an ETA, which we have never seen "
            "and currently ignore (locker orders are reported without a "
            "delivery window)."
            "\n  fields=%s",
            sorted(k for k in ("eta", "etaInformation") if raw.get(k)),
        )


# How deep the structure report walks. Budbee's deepest known nesting is four
# (``lockerAttributes.address.coordinate.latitude``); the cap is there so a
# self-referential or absurdly nested payload cannot turn a log line into a
# hang.
_MAX_STRUCTURE_DEPTH = 6


def describe_structure(value: Any, path: str = "", depth: int = 0) -> list[str]:
    """Return a payload's shape as ``path: type`` lines, **values omitted**.

    A response nobody has ever seen is worth more as a map than as a complaint:
    this is what tells us a home delivery's window is called
    ``consignment.start`` rather than that "something looks off". A list is
    described by its first element — the shape is the point, not the count —
    and only the *types* of leaves are reported, so an address, a name or a
    door code can never end up in a log a user pastes publicly.
    """
    if depth >= _MAX_STRUCTURE_DEPTH:
        return [f"{path}: … (nested deeper than {_MAX_STRUCTURE_DEPTH})"]
    if isinstance(value, dict):
        if not value:
            return [f"{path}: empty object"]
        lines: list[str] = []
        for key in sorted(value):
            child = f"{path}.{key}" if path else key
            lines += describe_structure(value[key], child, depth + 1)
        return lines
    if isinstance(value, list):
        if not value:
            return [f"{path}[]: empty list"]
        return describe_structure(value[0], f"{path}[]", depth + 1)
    return [f"{path}: {type(value).__name__}"]


def report_structure(raw: dict) -> None:
    """Log a home delivery's full shape, once per distinct shape.

    Only for home deliveries: the locker payload was read end to end from a
    real order, so dumping its structure would be noise. The door route never
    has been — every field name in this module's ``DELIVERY`` branch comes from
    reading Budbee's own tracking page, and one paste of this listing settles
    more than a year of "that field is missing" reports.

    Keyed on the shape itself rather than on "first response ever", so a
    delivered parcel that carries fields an in-transit one does not — a proof
    of delivery, a driver, an ETA — reports as well. Distinct shapes are few;
    this is not per-poll noise.
    """
    lines = describe_structure(raw)
    _warn_once(
        "structure:" + "|".join(lines),
        "Budbee home-delivery response structure (field names and types only, "
        "no values — safe to paste). No home delivery has ever been seen by "
        "this integration, so this listing is the single most useful thing you "
        "can send us, along with what Budbee's own page showed at the time:"
        "\n    %s",
        "\n    ".join(lines),
    )


def order_type(raw: dict) -> str:
    """Return ``BOX`` or ``DELIVERY`` for a raw payload.

    Prefers the keyless ``meta`` call the API client merges in — that is what
    Budbee itself routes on. ``consignment.type`` is **not** a substitute: it
    describes the shipment's direction and reads ``DELIVERY`` on locker orders
    too. Without a usable ``meta`` the payload's own shape decides: a locker
    order's ``status`` is a bare string, a door delivery's is
    ``{state, date}``.
    """
    meta = raw.get("meta")
    if isinstance(meta, dict):
        reported = meta.get("type")
        if reported in (ORDER_TYPE_BOX, ORDER_TYPE_DELIVERY):
            return str(reported)
    return ORDER_TYPE_DELIVERY if isinstance(raw.get("status"), dict) else ORDER_TYPE_BOX


def map_parcel_status(code: str | None, order: str = ORDER_TYPE_BOX) -> ParcelStatus:
    """Map a Budbee status to a canonical :class:`ParcelStatus`.

    ``None`` (a parcel Budbee has not registered yet) reports ``unknown``
    silently; an unrecognised code reports ``unknown`` with a one-shot warning.
    """
    if not code:
        return ParcelStatus.UNKNOWN
    status_map = (
        _STATUS_MAP_BOX if order == ORDER_TYPE_BOX else _STATUS_MAP_DELIVERY
    )
    mapped = status_map.get(code)
    if mapped is not None:
        return mapped
    _warn_unmapped_status(order, code)
    return ParcelStatus.UNKNOWN


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO 8601 string to an aware datetime, or ``None`` on failure.

    Naive values are treated as UTC so a list always sorts without crashing on
    a mixed set.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def to_iso_timestamp(value: Any) -> str | None:
    """Return an ISO 8601 string for a Budbee timestamp field.

    Budbee stamps ISO 8601 with a zone throughout, so strings pass through
    untouched (their consumers are guarded by :func:`parse_iso`). The numeric
    branch is kept for the suite's shared shape and treats numbers as epoch
    milliseconds.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    return str(value)


def unmasked(value: Any) -> str | None:
    """Return a text field, or ``None`` when Budbee masked or omitted it."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped or set(stripped) == {"*"}:
        return None
    return stripped


def tracking_url(tracking_code: str | None) -> str | None:
    """Construct the consumer tracking deep-link for a parcel."""
    if not tracking_code:
        return None
    return TRACKING_URL.format(tracking_code=tracking_code)


def is_outgoing(raw: dict) -> bool:
    """Whether this is something the user is *sending*, not receiving.

    Two signals, both keyless. ``consignment.type`` names the direction, and
    ``CollectedShippingLabel`` is a return label collected from the user, which
    only ever happens on an outgoing shipment.

    Neither has been seen on a live outgoing order — the one real parcel this
    mapping was built from was incoming — so the split ships alongside a
    warning in the coordinator the first time it fires.
    """
    consignment = raw.get("consignment")
    if isinstance(consignment, dict):
        if consignment.get("type") in OUTGOING_CONSIGNMENT_TYPES:
            return True
    return _raw_status(raw) == STATUS_COLLECTED_SHIPPING_LABEL


def _raw_status(raw: dict) -> str | None:
    """Return Budbee's own status string, from either payload shape."""
    status = raw.get("status")
    if isinstance(status, dict):
        state = status.get("state")
        return str(state) if state else None
    return str(status) if status else None


def _locker_name(raw: dict) -> str | None:
    """Return a human label for the locker an order is bound for.

    Budbee's lockers are pickup points in the canonical sense — exactly the
    call ``ha-inpost`` makes for a Paczkomat. A locker order carries the
    address twice (top level and under ``lockerAttributes``); a door delivery
    only ever under ``lockerAttributes``, and usually not at all.
    """
    candidates = [raw.get("lockerAddress")]
    attributes = raw.get("lockerAttributes")
    if isinstance(attributes, dict):
        candidates.append(attributes.get("address"))
    for address in candidates:
        if not isinstance(address, dict):
            continue
        name = unmasked(address.get("name"))
        if name:
            return name
        street = unmasked(address.get("street"))
        city = unmasked(address.get("city"))
        if street and city:
            return f"{street}, {city}"
        if street or city:
            return street or city
    return None


def normalize_parcel(raw: dict) -> dict:
    """Return a carrier-agnostic parcel dict with the payload under ``raw``.

    The **keys of the returned dict are the contract**: every carrier in the
    suite returns exactly these, in this order, and the aggregator and
    cross-carrier dashboards depend on it. A key Budbee does not expose is
    ``None``, never omitted.

    Budbee-specific decisions worth knowing before you change a line:

    * ``history`` is **always** ``None``. Budbee returns no event list on
      either route and every history sub-route 404s. A timeline accumulated
      locally from polls would differ per user depending on when they installed
      the integration, so we do not fake one.
    * ``receiver`` is normally ``None`` — it is the recipient's own name, which
      Budbee masks unless you hand it the e-mail address we deliberately never
      ask for. It is the HA user themselves anyway.
    * ``weight`` and ``dimensions`` are never exposed by this carrier.
    * On a locker order ``planned_from`` / ``planned_to`` are ``None``:
      ``latestPickupDate`` is a *collection deadline*, not a delivery window,
      and putting it there would make the calendar lie about when the parcel
      arrives. It stays under ``raw``.
    """
    order = order_type(raw)
    raw_status = _raw_status(raw)
    status = map_parcel_status(raw_status, order)
    delivered = status is ParcelStatus.DELIVERED

    if raw_status is not None:
        # Skipped for the placeholder a not-yet-registered code gets — that is
        # a normal state with no payload to have an opinion about.
        _warn_unverified_shapes(order, raw, status)

    if order == ORDER_TYPE_BOX:
        # Confirmed against a real NL locker order.
        tracking_code = raw.get("token")
        # Derive the collection moment from the status, never from the mere
        # presence of ``deliveredAt`` — on this route ``Delivered`` is the drop
        # into the locker, so that field can be set long before the user has
        # the parcel.
        delivered_at = to_iso_timestamp(raw.get("deliveredAt")) if delivered else None
        planned_from = planned_to = None
        receiver = unmasked(_get(raw, "consumer", "name"))
    else:
        # Reconstructed from Budbee's own tracking client; no door delivery has
        # been seen on the wire yet.
        tracking_code = raw.get("token")
        status_block = raw.get("status") if isinstance(raw.get("status"), dict) else {}
        delivered_at = to_iso_timestamp(status_block.get("date")) if delivered else None
        consignment = raw.get("consignment") or {}
        planned_from = to_iso_timestamp(consignment.get("start"))
        planned_to = to_iso_timestamp(consignment.get("stop"))
        if (
            planned_from
            and planned_to
            and parse_iso(planned_to) == parse_iso(planned_from)
        ):
            # Same instant twice is a point estimate, not a window.
            planned_to = None
        receiver = unmasked(_get(raw, "consumer", "name"))

    return {
        "carrier": "Budbee",
        "barcode": tracking_code,
        "sender": unmasked(_get(raw, "merchant", "name")),
        "receiver": receiver,
        "status": status,
        "raw_status": raw_status,
        "delivered": delivered,
        "delivered_at": delivered_at,
        "planned_from": None if delivered else planned_from,
        "planned_to": None if delivered else planned_to,
        "pickup": status is ParcelStatus.AT_PICKUP_POINT,
        "pickup_point": _locker_name(raw),
        "url": tracking_url(tracking_code),
        "weight": None,
        "dimensions": None,
        "history": None,
        "raw": raw,
    }


def _get(raw: dict, block: str, key: str) -> Any:
    """Return ``raw[block][key]`` when ``block`` is a dict, else ``None``."""
    value = raw.get(block)
    return value.get(key) if isinstance(value, dict) else None


def sort_parcels_by_ts(
    parcels: list[dict], key_field: str, *, descending: bool = False
) -> list[dict]:
    """Return normalised parcels sorted by the ISO timestamp at ``key_field``.

    The suite's sort contract: incoming/outgoing ascending on ``planned_from``,
    delivered descending on ``delivered_at``. Parcels whose value is missing or
    unparseable always sort to the end, regardless of ``descending``.
    """
    with_ts: list[tuple[datetime, dict]] = []
    without_ts: list[dict] = []
    for parcel in parcels:
        parsed = parse_iso(parcel.get(key_field))
        if parsed is None:
            without_ts.append(parcel)
        else:
            with_ts.append((parsed, parcel))
    with_ts.sort(key=lambda item: item[0], reverse=descending)
    return [parcel for _, parcel in with_ts] + without_ts


def apply_delivered_filter(parcels: list[dict], entry: ConfigEntry) -> list[dict]:
    """Trim the delivered list per the entry's retention option.

    ``parcels`` must already be sorted newest-first. ``days`` keeps deliveries
    from the last N days (an unparseable ``delivered_at`` is kept rather than
    silently dropped); the ``parcels`` type keeps the N most recent. Parcels
    stay *tracked* either way — this only controls what the delivered sensor
    shows.
    """
    options = entry.options
    filter_type = options.get(
        CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE
    )
    amount = int(
        options.get(CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT)
    )
    if filter_type == "days":
        cutoff = datetime.now(timezone.utc) - timedelta(days=amount)
        return [
            parcel
            for parcel in parcels
            if (parsed := parse_iso(parcel.get("delivered_at"))) is None
            or parsed >= cutoff
        ]
    return parcels[:amount]

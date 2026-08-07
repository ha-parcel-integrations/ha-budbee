"""Tests for the pure parcel-mapping helpers.

These need no Home Assistant instance — the whole point of keeping
``parcels.py`` free of I/O is that the carrier-specific mapping (the part you
rewrite per carrier) can be tested as plain functions.
"""
from datetime import datetime, timedelta, timezone

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.budbee.const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DOMAIN,
    ORDER_TYPE_BOX,
    ORDER_TYPE_DELIVERY,
    ParcelStatus,
)
from custom_components.budbee.parcels import (
    apply_delivered_filter,
    is_outgoing,
    map_parcel_status,
    normalize_parcel,
    order_type,
    parse_iso,
    sort_parcels_by_ts,
    to_iso_timestamp,
    unmasked,
)

from .payloads import (
    ACTIVE_CODE,
    DELIVERED_CODE,
    DELIVERY_CODE,
    box_sample,
    collected_sample,
    delivery_sample,
    outgoing_sample,
    waiting_sample,
)

# ---------------------------------------------------------------------------
# order_type — which status map applies
# ---------------------------------------------------------------------------


def test_order_type_comes_from_meta():
    assert order_type(box_sample()) == ORDER_TYPE_BOX
    assert order_type(delivery_sample()) == ORDER_TYPE_DELIVERY


def test_order_type_ignores_consignment_type():
    """``consignment.type`` is direction, not destination.

    A real locker order reports ``consignment.type: DELIVERY``. Reading that as
    the order type would apply the door map and announce a delivery while the
    parcel is still in the box.
    """
    raw = box_sample()
    assert raw["consignment"]["type"] == "DELIVERY"
    assert order_type(raw) == ORDER_TYPE_BOX


def test_order_type_falls_back_to_the_payload_shape():
    """Without a usable meta, the shape decides: a dict status is a door order."""
    box = box_sample()
    del box["meta"]
    assert order_type(box) == ORDER_TYPE_BOX

    door = delivery_sample()
    door["meta"] = {"type": "SOMETHING_NEW"}
    assert order_type(door) == ORDER_TYPE_DELIVERY

    assert order_type({"token": "X", "meta": "not-a-dict"}) == ORDER_TYPE_BOX


# ---------------------------------------------------------------------------
# map_parcel_status — two vocabularies
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code,expected",
    [
        ("NotStarted", ParcelStatus.REGISTERED),
        ("OnRouteCollection", ParcelStatus.IN_TRANSIT),
        ("Collected", ParcelStatus.IN_TRANSIT),
        ("CrossDocked", ParcelStatus.IN_TRANSIT),
        ("OnRouteDelivery", ParcelStatus.OUT_FOR_DELIVERY),
        ("Delivered", ParcelStatus.DELIVERED),
        ("Miss", ParcelStatus.PROBLEM),
        ("Backordered", ParcelStatus.PROBLEM),
        ("CollectedShippingLabel", ParcelStatus.IN_TRANSIT),
        ("ReturnedToTerminal", ParcelStatus.IN_TRANSIT),
        ("ReturnedToMerchant", ParcelStatus.RETURNING),
    ],
)
def test_delivery_status_map_is_complete(code, expected):
    assert map_parcel_status(code, ORDER_TYPE_DELIVERY) == expected


@pytest.mark.parametrize(
    "code,expected",
    [
        ("NotStarted", ParcelStatus.REGISTERED),
        ("Pending", ParcelStatus.REGISTERED),
        ("Collected", ParcelStatus.IN_TRANSIT),
        ("CollectedShippingLabel", ParcelStatus.IN_TRANSIT),
        ("DroppedOff", ParcelStatus.IN_TRANSIT),
        ("Delivered", ParcelStatus.AT_PICKUP_POINT),
        ("PickedUp", ParcelStatus.DELIVERED),
        ("Undelivered", ParcelStatus.PROBLEM),
        ("ReturnedToTerminal", ParcelStatus.IN_TRANSIT),
        ("ReturnedToMerchant", ParcelStatus.RETURNING),
    ],
)
def test_box_status_map_is_complete(code, expected):
    assert map_parcel_status(code, ORDER_TYPE_BOX) == expected


def test_delivered_means_different_things_per_order_type():
    """The trap this carrier is built around, asserted directly."""
    assert map_parcel_status("Delivered", ORDER_TYPE_BOX) == (
        ParcelStatus.AT_PICKUP_POINT
    )
    assert map_parcel_status("Delivered", ORDER_TYPE_DELIVERY) == (
        ParcelStatus.DELIVERED
    )


def test_map_parcel_status_missing_is_unknown():
    assert map_parcel_status(None) == ParcelStatus.UNKNOWN
    assert map_parcel_status("") == ParcelStatus.UNKNOWN


def test_map_parcel_status_unmapped_is_unknown():
    assert map_parcel_status("Teleported") == ParcelStatus.UNKNOWN


def test_unmapped_status_warns_once_per_order_type(caplog):
    assert map_parcel_status("Abducted", ORDER_TYPE_BOX) == ParcelStatus.UNKNOWN
    assert map_parcel_status("Abducted", ORDER_TYPE_BOX) == ParcelStatus.UNKNOWN
    assert caplog.text.count("Abducted") == 1
    assert "issues/new" in caplog.text
    # A value unknown on the other route is separate news.
    map_parcel_status("Abducted", ORDER_TYPE_DELIVERY)
    assert caplog.text.count("Abducted") == 2


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def test_parse_iso_handles_z_naive_and_garbage():
    assert parse_iso("2026-04-29T13:12:42Z").tzinfo is not None
    # A naive value is assumed UTC so mixed lists still sort.
    assert parse_iso("2026-04-29T13:12:42").tzinfo == timezone.utc
    assert parse_iso("not-a-date") is None
    assert parse_iso(None) is None


def test_to_iso_timestamp_passes_iso_through_and_converts_epochs():
    assert to_iso_timestamp("2026-04-29T13:12:42Z") == "2026-04-29T13:12:42Z"
    assert to_iso_timestamp(1784203767167) == "2026-07-16T12:09:27.167000+00:00"
    assert to_iso_timestamp(None) is None
    assert to_iso_timestamp(10**20) is None  # out of range -> None, never raises


def test_unmasked_treats_budbees_mask_as_absent():
    assert unmasked("Example Shop") == "Example Shop"
    assert unmasked("*****") is None
    assert unmasked("   ") is None
    assert unmasked(None) is None
    assert unmasked(42) is None


# ---------------------------------------------------------------------------
# is_outgoing
# ---------------------------------------------------------------------------


def test_incoming_locker_order_is_not_outgoing():
    assert is_outgoing(box_sample()) is False


@pytest.mark.parametrize("consignment_type", ["RETURN", "ON_DEMAND_PICKUP"])
def test_consignment_type_marks_a_shipment_outgoing(consignment_type):
    raw = box_sample()
    raw["consignment"]["type"] = consignment_type
    assert is_outgoing(raw) is True


def test_collected_shipping_label_marks_a_shipment_outgoing():
    raw = box_sample(status="CollectedShippingLabel")
    assert is_outgoing(raw) is True


def test_is_outgoing_survives_a_payload_without_a_consignment():
    assert is_outgoing({"token": "X"}) is False
    assert is_outgoing({"token": "X", "consignment": "nonsense"}) is False


# ---------------------------------------------------------------------------
# normalize_parcel — the canonical contract
# ---------------------------------------------------------------------------

CANONICAL_KEYS = [
    "carrier",
    "barcode",
    "sender",
    "receiver",
    "status",
    "raw_status",
    "delivered",
    "delivered_at",
    "planned_from",
    "planned_to",
    "pickup",
    "pickup_point",
    "url",
    "weight",
    "dimensions",
    "history",
    "raw",
]


def test_normalize_publishes_exactly_the_canonical_keys():
    """The aggregator and cross-carrier dashboards depend on this key set."""
    assert list(normalize_parcel(box_sample())) == CANONICAL_KEYS
    assert list(normalize_parcel(delivery_sample())) == CANONICAL_KEYS


def test_normalize_box_order_in_transit():
    parcel = normalize_parcel(box_sample())
    assert parcel["carrier"] == "Budbee"
    assert parcel["barcode"] == ACTIVE_CODE
    assert parcel["sender"] == "Example Shop"
    assert parcel["status"] == ParcelStatus.REGISTERED
    assert parcel["raw_status"] == "NotStarted"
    assert parcel["delivered"] is False
    assert parcel["url"] == f"https://track.budbee.com/{ACTIVE_CODE}"
    # A locker order exposes no delivery window at all: latestPickupDate is a
    # collection deadline, and it stays under raw.
    assert parcel["planned_from"] is None
    assert parcel["planned_to"] is None
    # Never exposed by this carrier.
    assert parcel["weight"] is None
    assert parcel["dimensions"] is None
    # No events[] on either route, and no locally invented timeline.
    assert parcel["history"] is None


def test_normalize_masks_are_not_names():
    """The keyless read masks the recipient — that must read as 'not exposed'."""
    parcel = normalize_parcel(box_sample())
    assert parcel["receiver"] is None
    assert normalize_parcel(delivery_sample())["receiver"] is None


def test_normalize_box_order_waiting_in_the_locker():
    parcel = normalize_parcel(waiting_sample())
    assert parcel["raw_status"] == "Delivered"
    # Budbee says "Delivered"; the parcel is in a hatch, not in the hallway.
    assert parcel["status"] == ParcelStatus.AT_PICKUP_POINT
    assert parcel["delivered"] is False
    assert parcel["delivered_at"] is None
    assert parcel["pickup"] is True
    assert parcel["pickup_point"] == "Budbee Box Example Supermarket"


def test_normalize_box_order_dropped_off_is_in_transit_not_pickup():
    """Regression: a locker drop-off is not the recipient's pickup point.

    ``DroppedOff`` is a person placing the parcel into a locker for a driver to
    collect — the sender's hand-over, hours before the parcel goes anywhere
    near the recipient's locker. Only ``Delivered`` means waiting for you.
    """
    parcel = normalize_parcel(box_sample(status="DroppedOff"))
    assert parcel["status"] == ParcelStatus.IN_TRANSIT
    assert parcel["pickup"] is False


def test_normalize_returned_to_terminal_is_in_transit_on_both_order_types():
    """Regression: "Returned" names terminal arrival, not a reversal."""
    box_parcel = normalize_parcel(box_sample(status="ReturnedToTerminal"))
    assert box_parcel["status"] == ParcelStatus.IN_TRANSIT
    assert box_parcel["pickup"] is False

    delivery_parcel = normalize_parcel(delivery_sample(state="ReturnedToTerminal"))
    assert delivery_parcel["status"] == ParcelStatus.IN_TRANSIT


def test_normalize_box_order_is_delivered_only_once_picked_up():
    parcel = normalize_parcel(collected_sample())
    assert parcel["status"] == ParcelStatus.DELIVERED
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] == "2026-04-29T13:12:42Z"
    assert parcel["barcode"] == DELIVERED_CODE


def test_normalize_locker_name_falls_back_to_the_address():
    raw = box_sample()
    raw["lockerAddress"]["name"] = None
    raw["lockerAttributes"]["address"]["name"] = None
    assert normalize_parcel(raw)["pickup_point"] == "Voorbeeldstraat 1, Amsterdam"

    raw["lockerAddress"]["street"] = None
    raw["lockerAttributes"]["address"]["street"] = None
    assert normalize_parcel(raw)["pickup_point"] == "Amsterdam"


def test_normalize_without_a_locker_has_no_pickup_point():
    raw = box_sample()
    del raw["lockerAddress"]
    del raw["lockerAttributes"]
    assert normalize_parcel(raw)["pickup_point"] is None


def test_normalize_delivery_order_has_the_consignment_window():
    parcel = normalize_parcel(delivery_sample())
    assert parcel["barcode"] == DELIVERY_CODE
    assert parcel["status"] == ParcelStatus.OUT_FOR_DELIVERY
    assert parcel["raw_status"] == "OnRouteDelivery"
    assert parcel["planned_from"] == "2026-04-29T13:00:00Z"
    assert parcel["planned_to"] == "2026-04-29T15:00:00Z"
    assert parcel["pickup"] is False
    assert parcel["pickup_point"] is None


def test_normalize_delivery_order_delivered_drops_the_window():
    parcel = normalize_parcel(delivery_sample(state="Delivered"))
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] == "2026-04-29T13:12:42Z"
    # The ETA is meaningless once the parcel has arrived.
    assert parcel["planned_from"] is None
    assert parcel["planned_to"] is None


def test_normalize_collapses_point_estimate_to_no_window_end():
    raw = delivery_sample()
    raw["consignment"]["stop"] = raw["consignment"]["start"]
    parcel = normalize_parcel(raw)
    assert parcel["planned_from"] == "2026-04-29T13:00:00Z"
    assert parcel["planned_to"] is None


def test_normalize_pending_placeholder():
    """A tracked-but-not-yet-registered code still yields a full parcel dict."""
    parcel = normalize_parcel({"token": "BUDBEE000001"})
    assert parcel["barcode"] == "BUDBEE000001"
    assert parcel["status"] == ParcelStatus.UNKNOWN
    assert parcel["delivered"] is False
    assert parcel["raw_status"] is None
    assert parcel["url"] == "https://track.budbee.com/BUDBEE000001"


def test_normalize_keeps_raw_payload():
    raw = box_sample()
    assert normalize_parcel(raw)["raw"] is raw


def test_normalize_outgoing_shipment():
    parcel = normalize_parcel(outgoing_sample())
    assert is_outgoing(parcel["raw"]) is True
    assert parcel["status"] == ParcelStatus.IN_TRANSIT


# ---------------------------------------------------------------------------
# sort_parcels_by_ts
# ---------------------------------------------------------------------------


def test_sort_parcels_ascending_puts_unparseable_last():
    parcels = [
        {"barcode": "a", "planned_from": "2026-05-02T10:00:00Z"},
        {"barcode": "b", "planned_from": None},
        {"barcode": "c", "planned_from": "2026-05-01T10:00:00Z"},
    ]
    ordered = [p["barcode"] for p in sort_parcels_by_ts(parcels, "planned_from")]
    assert ordered == ["c", "a", "b"]


def test_sort_parcels_descending_still_puts_unparseable_last():
    parcels = [
        {"barcode": "a", "delivered_at": "2026-05-02T10:00:00Z"},
        {"barcode": "b", "delivered_at": "nonsense"},
        {"barcode": "c", "delivered_at": "2026-05-01T10:00:00Z"},
    ]
    ordered = [
        p["barcode"]
        for p in sort_parcels_by_ts(parcels, "delivered_at", descending=True)
    ]
    assert ordered == ["a", "c", "b"]


# ---------------------------------------------------------------------------
# apply_delivered_filter
# ---------------------------------------------------------------------------


def _entry(filter_type: str, amount: int) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        options={
            CONF_DELIVERED_FILTER_TYPE: filter_type,
            CONF_DELIVERED_FILTER_AMOUNT: amount,
        },
        unique_id=DOMAIN,
    )


def _delivered_pair() -> list[dict]:
    now = datetime.now(timezone.utc)
    return [
        {"barcode": "RECENT", "delivered_at": (now - timedelta(days=1)).isoformat()},
        {"barcode": "OLD", "delivered_at": (now - timedelta(days=30)).isoformat()},
    ]


def test_delivered_filter_by_days():
    kept = apply_delivered_filter(_delivered_pair(), _entry("days", 7))
    assert [p["barcode"] for p in kept] == ["RECENT"]


def test_delivered_filter_by_count():
    parcels = _delivered_pair()
    assert apply_delivered_filter(parcels, _entry("parcels", 1)) == parcels[:1]


def test_delivered_filter_keeps_unparseable_timestamp():
    """Better to show a parcel with a broken date than to silently drop it."""
    parcels = [{"barcode": "WEIRD", "delivered_at": "nonsense"}]
    assert apply_delivered_filter(parcels, _entry("days", 7)) == parcels


# ---------------------------------------------------------------------------
# pre-1.0 reporting — every unverified shape has to ask for evidence
# ---------------------------------------------------------------------------


def test_unexpected_payload_field_is_reported(caplog):
    raw = box_sample()
    raw["somethingNew"] = {"nested": 1}
    normalize_parcel(raw)
    assert "somethingNew" in caplog.text
    assert "issues/new" in caplog.text
    # keys only — the payload carries an address
    assert "Voorbeeldstraat" not in caplog.text


def test_unexpected_payload_field_is_reported_once(caplog):
    raw = box_sample()
    raw["somethingNew"] = 1
    normalize_parcel(raw)
    normalize_parcel(raw)
    assert caplog.text.count("somethingNew") == 1


def test_first_home_delivery_asks_for_confirmation(caplog):
    """The whole DELIVERY branch is reconstructed; the first real one is news."""
    normalize_parcel(delivery_sample())
    assert "home delivery" in caplog.text
    assert "issues/new" in caplog.text


def test_locker_collection_asks_about_the_timestamp(caplog):
    """What ``deliveredAt`` means on a box order has never been observed."""
    normalize_parcel(collected_sample())
    assert "collected" in caplog.text.lower()


def test_unroutable_order_reports_the_guess(caplog):
    raw = box_sample()
    del raw["meta"]
    normalize_parcel(raw)
    assert "guessed" in caplog.text.lower()


def test_sibling_brand_is_reported(caplog):
    """Instabox/Porterbuddy share this backend — confirmation is wanted."""
    raw = box_sample()
    raw["meta"] = {**raw["meta"], "brand": "PORTERBUDDY"}
    normalize_parcel(raw)
    assert "PORTERBUDDY" in caplog.text


def test_eta_on_a_locker_order_is_reported(caplog):
    """We report locker orders without a window; an ETA would change that."""
    raw = box_sample()
    raw["eta"] = {"date": "2026-04-29T13:40:00Z"}
    normalize_parcel(raw)
    assert "ETA" in caplog.text


def test_pending_placeholder_reports_nothing(caplog):
    """A not-yet-registered code is a normal state, not a finding."""
    normalize_parcel({"token": "BUDBEE000001"})
    assert caplog.text == ""


def test_structure_report_lists_types_never_values(caplog):
    normalize_parcel(delivery_sample())
    assert "consignment.start: str" in caplog.text
    assert "status.state: str" in caplog.text
    # nothing from the payload's own values, ever
    assert "OnRouteDelivery" not in caplog.text
    assert "Example Shop" not in caplog.text


def test_structure_report_fires_again_for_a_different_shape(caplog):
    normalize_parcel(delivery_sample())
    first = caplog.text.count("response structure")
    normalize_parcel(delivery_sample())  # same shape: silent
    assert caplog.text.count("response structure") == first

    richer = delivery_sample()
    richer["proofOfDelivery"] = {"photoUrl": "https://example.test/x.jpg"}
    normalize_parcel(richer)
    assert caplog.text.count("response structure") == first + 1


def test_structure_report_describes_lists_by_their_first_element():
    from custom_components.budbee.parcels import describe_structure

    lines = describe_structure({"events": [{"date": "x", "n": 1}], "empty": []})
    assert "events[].date: str" in lines
    assert "events[].n: int" in lines
    assert "empty[]: empty list" in lines


def test_structure_report_stops_at_the_depth_cap():
    from custom_components.budbee.parcels import describe_structure

    deep: dict = {"a": {"b": {"c": {"d": {"e": {"f": {"g": 1}}}}}}}
    assert any("nested deeper than" in line for line in describe_structure(deep))


def test_locker_orders_do_not_dump_their_structure(caplog):
    """The box payload is confirmed; a full dump there would be noise."""
    normalize_parcel(box_sample())
    assert "response structure" not in caplog.text


def test_live_locker_fields_are_known(caplog):
    """Fields a real locker order turned out to carry must not warn."""
    normalize_parcel(box_sample())
    assert "lockerBrands" not in caplog.text

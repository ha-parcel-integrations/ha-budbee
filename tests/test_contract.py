"""The parcel contract's checks, run over every normalised sample."""

from datetime import datetime

import pytest

from custom_components.budbee.const import ParcelStatus
from custom_components.budbee.parcels import normalize_parcel, parse_iso

from .payloads import (
    box_sample,
    collected_sample,
    delivery_sample,
    outgoing_sample,
    waiting_sample,
)

CANONICAL_KEYS = [
    "carrier",
    "barcode",
    "sender",
    "receiver",
    "contents",
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


def _eta_box() -> dict:
    sample = box_sample(status="ReturnedToTerminal")
    sample["eta"] = "2099-04-29T10:10:00Z"
    return sample


def _door_moment() -> dict:
    sample = delivery_sample()
    sample["consignment"]["start"] = "2099-04-29T13:00:00Z"
    sample["consignment"]["stop"] = None
    return sample


def _door_date() -> dict:
    sample = delivery_sample()
    sample["consignment"]["start"] = "2099-04-29"
    sample["consignment"]["stop"] = None
    return sample


SAMPLES = {
    "box_registered": box_sample,
    "box_eta": _eta_box,
    "box_waiting": waiting_sample,
    "box_collected": collected_sample,
    "box_outgoing": outgoing_sample,
    "box_returning": lambda: box_sample(status="ReturnedToMerchant"),
    "box_problem": lambda: box_sample(status="Undelivered"),
    "door_active": delivery_sample,
    "door_delivered": lambda: delivery_sample(state="Delivered"),
    "door_returning": lambda: delivery_sample(state="ReturnedToMerchant"),
    "door_problem": lambda: delivery_sample(state="Miss"),
    "door_moment": _door_moment,
    "door_date": _door_date,
    "bare": lambda: {"token": "BUDBEE000001"},
}


@pytest.fixture(params=SAMPLES)
def parcel(request) -> dict:
    return normalize_parcel(SAMPLES[request.param]())


def test_keys_and_order(parcel):
    assert list(parcel) == CANONICAL_KEYS


def test_delivered_follows_status(parcel):
    assert parcel["delivered"] is (parcel["status"] is ParcelStatus.DELIVERED)


def test_delivered_at_only_when_delivered(parcel):
    if not parcel["delivered"]:
        assert parcel["delivered_at"] is None


def test_planned_window_is_ordered_and_anchored(parcel):
    if parcel["planned_to"] is not None:
        assert parcel["planned_from"] is not None
        assert parse_iso(parcel["planned_to"]) > parse_iso(parcel["planned_from"])


def test_planned_is_empty_when_the_parcel_is_done_or_waiting(parcel):
    if parcel["status"] in (
        ParcelStatus.DELIVERED,
        ParcelStatus.AT_PICKUP_POINT,
        ParcelStatus.RETURNING,
    ):
        assert parcel["planned_from"] is None
        assert parcel["planned_to"] is None


def test_pickup_is_a_bool_and_the_point_needs_it(parcel):
    assert isinstance(parcel["pickup"], bool)
    if not parcel["pickup"]:
        assert parcel["pickup_point"] is None


def test_a_collected_locker_parcel_stays_pickup():
    parcel = normalize_parcel(collected_sample())
    assert parcel["delivered"] is True
    assert parcel["pickup"] is True
    assert parcel["pickup_point"] == "Budbee Box Example Supermarket"


def test_a_door_delivery_is_not_pickup():
    parcel = normalize_parcel(delivery_sample())
    assert parcel["pickup"] is False
    assert parcel["pickup_point"] is None


def test_a_locker_eta_runs_to_the_end_of_that_local_day():
    parcel = normalize_parcel(_eta_box())
    assert parcel["planned_from"] == "2099-04-29T10:10:00Z"
    end = datetime.fromisoformat(parcel["planned_to"])
    assert (end.hour, end.minute, end.second, end.microsecond) == (23, 59, 59, 0)
    assert end.date() == parse_iso(parcel["planned_from"]).astimezone(end.tzinfo).date()


def test_a_moment_ends_at_the_end_of_that_day():
    parcel = normalize_parcel(_door_moment())
    end = datetime.fromisoformat(parcel["planned_to"])
    assert (end.hour, end.minute, end.second, end.microsecond) == (23, 59, 59, 0)


def test_a_date_covers_the_whole_day():
    parcel = normalize_parcel(_door_date())
    start = datetime.fromisoformat(parcel["planned_from"])
    end = datetime.fromisoformat(parcel["planned_to"])
    assert (start.hour, start.minute, start.second) == (0, 0, 0)
    assert (end.hour, end.minute, end.second, end.microsecond) == (23, 59, 59, 0)
    assert start.date() == end.date()

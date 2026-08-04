"""Sample Budbee API payloads shared by the test modules.

The ``BOX`` samples are the shape of a **real** NL locker order, with every
personal field replaced. The recipient block deliberately keeps Budbee's own
``*****`` mask, because that is what the keyless read actually returns and the
mapping has to treat it as "not exposed" rather than as a name.

The ``DELIVERY`` sample is **reconstructed** — no door delivery has been read
on the wire — so treat a test built on it as a guard against regressions in our
own mapping, not as proof of the carrier's shape.

Kept in one module rather than inline in each test: when the payload turns out
to differ from what we assumed, there is then exactly one place to fix.
"""
from __future__ import annotations

ACTIVE_CODE = "BUDBEE999999"
DELIVERED_CODE = "BUDBEE123456"
DELIVERY_CODE = "BUDBEE555555"

MASK = "*****"

# What GET /v3/orders/{code}/meta answers, keylessly, for each order type.
META_BOX = {
    "type": "BOX",
    "country": "NL",
    "brand": "BUDBEE",
    "defaultLocale": "nl_NL",
    "system": "INSTABOX",
}
META_DELIVERY = {**META_BOX, "type": "DELIVERY"}


def envelope(payload: object) -> dict:
    """Wrap a payload the way every ``/v3/*`` route does."""
    return {
        "status": "COMPLETED",
        "payload": payload,
        "errorCode": None,
        "errorMsg": None,
    }


def not_found() -> dict:
    """The failure envelope an unknown code gets — inside an HTTP 200."""
    return {
        "status": "FAILED",
        "payload": None,
        "errorCode": "ORDER_NOT_FOUND",
        "errorMsg": "Order not found",
    }


def box_sample(code: str = ACTIVE_CODE, status: str = "NotStarted") -> dict:
    """A locker order, as ``GET /box/{code}`` returns it: flat, no envelope."""
    locker_address = {
        "name": "Budbee Box Example Supermarket",
        "street": "Voorbeeldstraat 1",
        "city": "Amsterdam",
        "postalCode": "1000 AA",
        "countryCode": "NL",
        "coordinate": {"latitude": 52.0, "longitude": 4.0},
        "directions": "Inside, to the left of the entrance",
    }
    return {
        "authorized": False,
        "token": code,
        "createdAt": "2026-04-27T09:14:02Z",
        "timeZone": "Europe/Amsterdam",
        "status": status,
        "pickedUpFromMerchant": False,
        "deliveredAt": None,
        "latestPickupDate": None,
        "eta": None,
        "etaInformation": None,
        "orderLocation": {
            "parcelsPickedUpFromMerchant": False,
            "distributionCity": "Budbee Example City",
        },
        # DELIVERY here means "towards the consumer", not "to the door" — this
        # is a locker order all the same.
        "consignment": {"type": "DELIVERY", "cancellation": None, "stop": None},
        "recall": {"recalled": False, "reason": None},
        "returns": {
            "enabled": False,
            "returnableParcelCount": 0,
            "photo": None,
            "allowedToCancelReservation": False,
        },
        "merchant": {
            "name": "Example Shop",
            "logo": None,
            "supportEmail": None,
            "supportPhone": None,
            "supportLink": None,
        },
        "lockerIdentifier": "NL-LOCKER-001",
        "lockerAddress": dict(locker_address),
        "lockerAttributes": {
            "identifier": "NL-LOCKER-001",
            "address": dict(locker_address),
            "acceptedBrands": [],
            "color": "GREEN",
            "visualBranding": "Budbee",
            "demand": {"level": "NORMAL", "title": None, "text": None},
            "openHours": {"monday": {"open": "08:00", "close": "22:00"}},
            "entryAccessCode": None,
        },
        "openHours": {"monday": {"open": "08:00", "close": "22:00"}},
        "demand": {"level": "NORMAL", "title": None, "text": None},
        # Masked unless the per-order auth code is sent, which we never do.
        "consumer": {"name": MASK, "email": MASK, "phoneNumber": MASK},
        "consumerAddress": {
            "street": MASK,
            "street2": MASK,
            "postalCode": MASK,
            "city": MASK,
            "countryCode": "NL",
            "coordinate": None,
            "settings": None,
        },
        "identificationAtLocker": None,
        "rating": None,
        "campaign": None,
        "nudges": [],
        "canSwitchDeliveryType": False,
        "canSwitchLocker": False,
        "canExtendPickupTime": False,
        "meta": dict(META_BOX),
    }


def waiting_sample(code: str = ACTIVE_CODE) -> dict:
    """A locker order sitting in the box — Budbee's ``Delivered``.

    The trap this carrier is built around: on this route ``Delivered`` means
    *in the locker*, not *handed over*.
    """
    sample = box_sample(code, status="Delivered")
    sample["deliveredAt"] = "2026-04-29T13:12:42Z"
    sample["latestPickupDate"] = "2026-05-02T13:12:42Z"
    return sample


def collected_sample(code: str = DELIVERED_CODE) -> dict:
    """A locker order the user has actually collected — ``PickedUp``."""
    sample = waiting_sample(code)
    sample["status"] = "PickedUp"
    return sample


def outgoing_sample(code: str = ACTIVE_CODE) -> dict:
    """A locker-to-locker return the user is sending."""
    sample = box_sample(code, status="Collected")
    sample["consignment"] = {"type": "RETURN", "cancellation": None, "stop": None}
    return sample


def delivery_sample(code: str = DELIVERY_CODE, state: str = "OnRouteDelivery") -> dict:
    """A door delivery's conspectus — reconstructed, never seen on the wire."""
    return {
        "token": code,
        "createdAt": "2026-04-27T09:14:02Z",
        "timeZone": "Europe/Amsterdam",
        "status": {"state": state, "date": "2026-04-29T13:12:42Z"},
        "consignment": {
            "type": "DELIVERY",
            "start": "2026-04-29T13:00:00Z",
            "stop": "2026-04-29T15:00:00Z",
            "routingDeadline": "2026-04-28T20:00:00Z",
            "cancellation": None,
        },
        "routedEta": None,
        "eta": {"date": "2026-04-29T13:40:00Z", "remainingStopCount": 4},
        "merchant": {
            "name": "Example Shop",
            "logo": None,
            "supportEmail": None,
            "supportPhone": None,
            "supportLink": None,
        },
        "consumer": {"name": MASK, "phoneNumber": MASK},
        "address": {
            "street": MASK,
            "street2": MASK,
            "postalCode": MASK,
            "city": MASK,
            "countryCode": "NL",
            "coordinate": None,
            "settings": None,
        },
        "driver": {"id": 1234, "name": MASK, "icon": None},
        "orderLocation": {"parcelsPickedUpFromMerchant": True},
        "identification": {"code": None, "needsVerification": False},
        "deliveryPinCode": None,
        "settings": {"outsideDoorOk": False, "knockOnDoor": True},
        "deliverySettings": {"accessMode": None, "estateType": "HOUSE"},
        "returns": None,
        "recall": None,
        "nudges": [],
        "cancellation": None,
        "latestPickupDate": None,
        "canSwitchDeliveryType": False,
        "canSwitchLocker": False,
        "canExtendPickupTime": False,
        "meta": dict(META_DELIVERY),
    }

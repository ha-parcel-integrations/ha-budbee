"""Constants for the Budbee parcel tracker integration."""
from enum import StrEnum

from homeassistant.const import Platform

DOMAIN = "budbee"


class ParcelStatus(StrEnum):
    """Carrier-agnostic parcel status.

    **Do not extend or rename these members.** Every integration in the parcel
    suite publishes exactly this vocabulary on the ``status`` field of each
    normalised parcel, so cross-carrier automations and the aggregator can
    target ``status: out_for_delivery`` regardless of carrier. Listed in
    roughly the order a parcel moves through.
    """

    REGISTERED = "registered"               # Sender announced the parcel; not handed over yet
    IN_TRANSIT = "in_transit"               # In the carrier's network
    OUT_FOR_DELIVERY = "out_for_delivery"   # On a delivery vehicle today
    AT_PICKUP_POINT = "at_pickup_point"     # Ready to collect at a pickup location
    DELIVERED = "delivered"                 # Handed over
    RETURNING = "returning"                 # Failed delivery, going back to sender
    PROBLEM = "problem"                     # Carrier reports an exception/issue
    UNKNOWN = "unknown"                     # Raw status we have not mapped yet


PLATFORMS = [Platform.BUTTON, Platform.CALENDAR, Platform.SENSOR]

# Where users report something we do not recognise yet — an unmapped status, an
# order type outside the two known ones, a payload shape we did not expect.
# Lives here rather than in one module because both the API client and the
# parcel mapping log it.
#
# The ``?template=`` parameter matters: without it the link opens a blank form,
# and the report comes back missing the version and the log line we need.
NEW_ISSUE_URL = (
    "https://github.com/ha-parcel-integrations/ha-budbee/issues/new"
    "?template=unrecognised_status.yml"
)

# Budbee's own consumer tracking host. No key, no cookie, no auth header: the
# tracking code alone is the credential. Reading an order takes two calls —
# ``META_URL`` routes it, then one of the two read URLs below — because Budbee
# serves locker orders and door deliveries from different routes with different
# response shapes *and* different status vocabularies.
#
# The e-mail Budbee's own tracking page asks for is deliberately not collected:
# it unmasks the recipient's own name, address and phone in the response and
# nothing a parcel sensor reads. See CLAUDE.md; the full write-up lives in this
# carrier's directory under the private ``carrier-research/api/``.
API_BASE = "https://tracking.budbee.com/api"
META_URL = f"{API_BASE}/v3/orders/{{tracking_code}}/meta"
BOX_URL = f"{API_BASE}/box/{{tracking_code}}"
ORDER_URL = f"{API_BASE}/v3/orders/{{tracking_code}}"
TRACKING_URL = "https://track.budbee.com/{tracking_code}"

# ``meta.type`` — which read route serves the order, and which status map
# applies to it. A closed two-value enum.
ORDER_TYPE_BOX = "BOX"            # to a locker; read via BOX_URL
ORDER_TYPE_DELIVERY = "DELIVERY"  # to the door; read via ORDER_URL

# The envelope every ``/v3/*`` route uses reports failures inside an HTTP 200,
# so the body's ``errorCode`` — not the status line — says whether the code is
# known. ``/box/`` is the exception: it answers a real 404.
ERROR_ORDER_NOT_FOUND = "ORDER_NOT_FOUND"

# ``consignment.type`` describes the *direction* of the shipment, not its
# destination — it reads ``DELIVERY`` on locker orders too. These two values are
# the user sending something rather than receiving it.
OUTGOING_CONSIGNMENT_TYPES = frozenset({"RETURN", "ON_DEMAND_PICKUP"})

# A return label collected from the user is outgoing whatever the consignment
# says, and it is the one outgoing signal that appears in both status maps.
STATUS_COLLECTED_SHIPPING_LABEL = "CollectedShippingLabel"

# Tracked parcels live in the config entry options as a list of
# ``{tracking_code}`` dicts — this carrier has no account or parcel feed, so the
# user enters the codes themselves. Kept as dicts so future per-parcel fields
# slot in without an options migration.
CONF_PARCELS = "parcels"
CONF_TRACKING_CODE = "tracking_code"

# Delivered-parcels retention: keep delivered parcels visible for the last N
# days, or keep only the N most recent — identical across the suite.
CONF_DELIVERED_FILTER_TYPE = "delivered_filter_type"
CONF_DELIVERED_FILTER_AMOUNT = "delivered_filter_amount"
DEFAULT_DELIVERED_FILTER_TYPE = "days"
DEFAULT_DELIVERED_FILTER_AMOUNT = 7

# Refresh interval (minutes) controls how often the coordinator polls the
# carrier. Default 30 min keeps the load on a consumer endpoint gentle; the
# minimum is 15 min for the same reason.
#
# Deliberate divergence from the HA Core rule that polling intervals are not
# user-configurable: that rule targets core integrations, and in a HACS parcel
# tracker a tunable cadence is a wanted feature. Generate with
# ``--interval fixed`` instead when the carrier throttles or soft-bans unusual
# traffic — that drops the option entirely and hard-codes the cadence, so users
# cannot dial it down to something that gets them blocked.
CONF_REFRESH_INTERVAL = "refresh_interval"
REFRESH_INTERVAL_OPTIONS = (15, 30, 60, 120, 240)
DEFAULT_REFRESH_INTERVAL = 30

# No ``include_history`` option here, unlike every other carrier in the suite.
# Budbee returns no event list on either read route and every history sub-route
# 404s, so the ``history`` key is permanently ``None`` — see ``parcels.py``. An
# opt-in toggle that can never produce anything is worse than no toggle, and
# accumulating a timeline locally from polls was rejected: it would differ per
# user depending on when they installed the integration.

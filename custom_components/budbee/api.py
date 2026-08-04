"""Budbee public tracking API client.

Reading one parcel takes two calls, and the first one is not optional:

``GET /v3/orders/{code}/meta``
    The router. Returns ``{type, country, brand, defaultLocale, system}`` with
    no credential. ``type`` picks both the read route below *and* the status
    vocabulary — Budbee runs two, in which ``Delivered`` means different things.

``GET /box/{code}`` (``type == BOX``) or ``GET /v3/orders/{code}`` (``DELIVERY``)
    The parcel itself. The routes are not interchangeable: a locker order is
    ``ORDER_NOT_FOUND`` on the orders route and vice versa.

Two response conventions live side by side, so the client cannot use one check
for both. The ``/v3/*`` routes answer **HTTP 200 with the failure in the body**
(``{"status": "FAILED", "errorCode": "ORDER_NOT_FOUND"}``), while ``/box/``
answers a real ``404``.

The contract the coordinator relies on is unchanged from the rest of the suite:

* ``async_get_parcel`` returns the raw per-parcel dict on success,
* returns ``None`` when Budbee says the tracking code is unknown or not yet
  scanned (a normal, expected state — never an error),
* raises :class:`BudbeeApiError` for anything else,
* lets ``aiohttp.ClientError`` propagate untouched — ``DataUpdateCoordinator``
  already wraps those into ``UpdateFailed``.

The returned dict is the carrier's own payload with the ``meta`` response
merged in under a ``meta`` key (neither read route carries one of its own), so
the mapping in :mod:`.parcels` can pick the right status map from the same dict
it normalises, and diagnostics show what the order was routed as.
"""
from __future__ import annotations

import logging
from typing import Any

import aiohttp

from .const import (
    BOX_URL,
    ERROR_ORDER_NOT_FOUND,
    META_URL,
    NEW_ISSUE_URL,
    ORDER_TYPE_BOX,
    ORDER_TYPE_DELIVERY,
    ORDER_URL,
)

_LOGGER = logging.getLogger(__name__)

# Everything already reported this HA session, so each finding is logged once
# rather than on every poll.
_warned: set[str] = set()


def _warn_once(key: str, message: str, *args: Any) -> None:
    """Log ``message`` as a WARNING the first time ``key`` comes up.

    WARNING and not INFO/DEBUG on purpose: Home Assistant's default log level
    hides those, and nobody reports what they never see. Keys and types only —
    these payloads carry an address and, on a door delivery, a door code.
    """
    if key in _warned:
        return
    _warned.add(key)
    _LOGGER.warning(message + "\n  Please report it: %s", *args, NEW_ISSUE_URL)


class BudbeeApiError(Exception):
    """Raised when a Budbee API call returns an unexpected response."""

    def __init__(self, detail: str) -> None:
        """Store the detail that triggered the error."""
        super().__init__(f"Budbee API request failed: {detail}")
        self.detail = detail


class BudbeeApiClient:
    """Client for the public Budbee tracking endpoints.

    No authentication. Budbee's tracking page asks for an e-mail address as
    well, but that only unmasks the recipient's own name, address and phone in
    the response — every field a parcel sensor reads is served without it, so
    this client never sends one.
    """

    def __init__(self, session: aiohttp.ClientSession) -> None:
        """Initialise the client with an aiohttp session."""
        self._session = session
        # tracking_code -> meta payload. An order does not change type, so the
        # router runs once per code and every later poll is a single request.
        self._meta_cache: dict[str, dict[str, Any]] = {}

    def forget(self, tracking_code: str) -> None:
        """Drop a code's cached routing, so a re-added parcel is re-routed."""
        self._meta_cache.pop(tracking_code, None)

    async def async_get_parcel(self, tracking_code: str) -> dict[str, Any] | None:
        """Fetch one parcel's tracking details.

        Returns the parcel dict for a known parcel, or ``None`` when Budbee
        reports the code as unknown — which is also what a code that has not
        been handed to Budbee yet gets. Any other failure raises
        :class:`BudbeeApiError`; network errors propagate as
        ``aiohttp.ClientError``.
        """
        meta = self._meta_cache.get(tracking_code)
        if meta is None:
            meta = await self._async_get_meta(tracking_code)
            if meta is None:
                return None
            self._meta_cache[tracking_code] = meta

        order_type = meta.get("type")
        if order_type == ORDER_TYPE_BOX:
            payload = await self._async_get_box(tracking_code)
        elif order_type == ORDER_TYPE_DELIVERY:
            payload = await self._async_get_conspectus(tracking_code)
        else:
            # Both known types are covered above, so this is genuinely new.
            # Try both routes rather than dropping the parcel — the mapping
            # falls back to detecting the payload shape when the type is one
            # it does not know.
            _warn_unknown_order_type(order_type)
            payload = await self._async_get_box(tracking_code)
            if payload is None:
                payload = await self._async_get_conspectus(tracking_code)

        if payload is None:
            return None
        return {**payload, "meta": meta}

    async def _async_get_meta(self, tracking_code: str) -> dict[str, Any] | None:
        """Route the order: which read endpoint and which status map it uses."""
        body = await self._async_get_json(META_URL.format(tracking_code=tracking_code))
        return self._unwrap(body, tracking_code)

    async def _async_get_box(self, tracking_code: str) -> dict[str, Any] | None:
        """Read a locker order. Flat object, no envelope, real 404 if unknown."""
        body = await self._async_get_json(
            BOX_URL.format(tracking_code=tracking_code), not_found_status=404
        )
        if body is None:
            return None
        if not isinstance(body, dict):
            raise BudbeeApiError("unexpected box body (not a JSON object)")
        return body

    async def _async_get_conspectus(self, tracking_code: str) -> dict[str, Any] | None:
        """Read a door delivery. Enveloped, with the parcel under ``conspectus``."""
        body = await self._async_get_json(ORDER_URL.format(tracking_code=tracking_code))
        payload = self._unwrap(body, tracking_code)
        if payload is None:
            return None
        conspectus = payload.get("conspectus")
        if isinstance(conspectus, dict):
            return conspectus
        # No door delivery has been seen on the wire yet, so an envelope
        # without a conspectus is news worth a warning rather than a silent
        # drop. Log the keys, never the values — this payload carries an
        # address and a door code.
        _warn_once(
            "conspectus:missing",
            "Budbee returned a home delivery in a shape we did not expect: no "
            "'conspectus' in the envelope. The parcel is still read, but its "
            "fields may be mapped wrongly."
            "\n  keys=%s",
            sorted(payload),
        )
        return payload

    async def _async_get_json(
        self, url: str, *, not_found_status: int | None = None
    ) -> Any:
        """GET ``url`` and parse the body, or ``None`` for a semantic 404."""
        async with self._session.get(url) as response:
            if not_found_status is not None and response.status == not_found_status:
                return None
            if response.status != 200:
                raise BudbeeApiError(f"HTTP {response.status}")
            try:
                # content_type=None: consumer endpoints routinely serve JSON as
                # text/plain, and aiohttp would otherwise refuse to parse it.
                return await response.json(content_type=None)
            except ValueError as err:
                raise BudbeeApiError(f"unparseable body ({err})") from err

    def _unwrap(self, body: Any, tracking_code: str) -> dict[str, Any] | None:
        """Return the payload of a ``/v3/*`` envelope, or ``None`` if unknown.

        The envelope reports failure inside an HTTP 200, so this — not the
        status line — is where a bad code is detected.
        """
        if not isinstance(body, dict):
            raise BudbeeApiError("unexpected body (not a JSON object)")

        error = body.get("errorCode")
        if error == ERROR_ORDER_NOT_FOUND:
            return None
        if error:
            raise BudbeeApiError(str(error))
        if body.get("status") == "FAILED":
            raise BudbeeApiError(str(body.get("errorMsg") or "FAILED"))

        payload = body.get("payload")
        if not isinstance(payload, dict):
            # A success envelope must carry a payload; treat a hollow one as
            # unknown rather than failing the whole poll. The tracking code is
            # not logged — it is the only credential this API has.
            _warn_once(
                "envelope:hollow",
                "Budbee answered a lookup with a success envelope but no "
                "payload. The parcel is shown as pending."
                "\n  keys=%s",
                sorted(body),
            )
            return None
        return payload


def _warn_unknown_order_type(order_type: Any) -> None:
    """Log an order type outside the known two once, with an issue link."""
    _warn_once(
        f"order-type:{order_type}",
        "Unrecognised Budbee order type — help us map it. Budbee has only ever "
        "reported BOX and DELIVERY, so this is a route we do not know."
        "\n  meta.type=%s → trying both read routes",
        str(order_type),
    )

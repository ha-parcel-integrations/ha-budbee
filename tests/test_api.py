"""Tests for the Budbee API client."""
import json
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.budbee.api import BudbeeApiClient, BudbeeApiError

from .payloads import (
    ACTIVE_CODE,
    META_BOX,
    META_DELIVERY,
    box_sample,
    delivery_sample,
    envelope,
    not_found,
)

CODE = ACTIVE_CODE


def _response(status: int, body: object) -> AsyncMock:
    response = AsyncMock()
    response.status = status
    if isinstance(body, str):
        response.json = AsyncMock(side_effect=json.JSONDecodeError("x", body, 0))
    else:
        response.json = AsyncMock(return_value=body)
    return response


def _session(*replies: tuple[int, object]) -> MagicMock:
    """A session answering the given (status, body) pairs, in order.

    The last reply is repeated, so a test that only cares about one call does
    not have to spell out the routing call twice.
    """
    responses = [_response(status, body) for status, body in replies]

    def _get(url: str):
        response = responses.pop(0) if len(responses) > 1 else responses[0]
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=response)
        ctx.__aexit__ = AsyncMock(return_value=False)
        return ctx

    session = MagicMock()
    session.get = MagicMock(side_effect=_get)
    return session


def _urls(session: MagicMock) -> list[str]:
    return [call.args[0] for call in session.get.call_args_list]


async def test_box_order_is_routed_to_the_box_endpoint():
    session = _session((200, envelope(META_BOX)), (200, box_sample(CODE)))
    client = BudbeeApiClient(session)

    parcel = await client.async_get_parcel(CODE)

    assert parcel["token"] == CODE
    assert parcel["meta"]["type"] == "BOX"
    meta_url, read_url = _urls(session)
    assert meta_url.endswith(f"/v3/orders/{CODE}/meta")
    assert read_url.endswith(f"/box/{CODE}")


async def test_delivery_order_is_routed_to_the_orders_endpoint():
    session = _session(
        (200, envelope(META_DELIVERY)),
        (200, envelope({"conspectus": delivery_sample(CODE)})),
    )
    client = BudbeeApiClient(session)

    parcel = await client.async_get_parcel(CODE)

    assert parcel["status"]["state"] == "OnRouteDelivery"
    assert parcel["meta"]["type"] == "DELIVERY"
    assert _urls(session)[1].endswith(f"/v3/orders/{CODE}")


async def test_meta_is_cached_so_later_polls_are_one_request():
    session = _session((200, envelope(META_BOX)), (200, box_sample(CODE)))
    client = BudbeeApiClient(session)

    await client.async_get_parcel(CODE)
    session.get.reset_mock()
    await client.async_get_parcel(CODE)

    assert len(_urls(session)) == 1
    assert _urls(session)[0].endswith(f"/box/{CODE}")


async def test_forget_makes_the_client_route_again():
    session = _session(
        (200, envelope(META_BOX)),
        (200, box_sample(CODE)),
        (200, envelope(META_BOX)),
        (200, box_sample(CODE)),
    )
    client = BudbeeApiClient(session)

    await client.async_get_parcel(CODE)
    client.forget(CODE)
    client.forget("never-seen")  # no-op, must not raise
    session.get.reset_mock()
    await client.async_get_parcel(CODE)

    assert len(_urls(session)) == 2


async def test_unknown_code_is_none_not_an_error():
    """ORDER_NOT_FOUND arrives inside an HTTP 200 — a normal state."""
    client = BudbeeApiClient(_session((200, not_found())))
    assert await client.async_get_parcel(CODE) is None


async def test_box_route_reports_an_unknown_order_as_a_real_404():
    client = BudbeeApiClient(_session((200, envelope(META_BOX)), (404, None)))
    assert await client.async_get_parcel(CODE) is None


async def test_unknown_order_type_tries_both_read_routes(caplog):
    session = _session(
        (200, envelope({**META_BOX, "type": "SOMETHING_NEW"})),
        (404, None),
        (200, envelope({"conspectus": delivery_sample(CODE)})),
    )
    client = BudbeeApiClient(session)

    parcel = await client.async_get_parcel(CODE)

    assert parcel["token"] == CODE
    assert [url.rsplit("/api", 1)[1] for url in _urls(session)] == [
        f"/v3/orders/{CODE}/meta",
        f"/box/{CODE}",
        f"/v3/orders/{CODE}",
    ]
    assert "SOMETHING_NEW" in caplog.text


async def test_unknown_order_type_returns_none_when_neither_route_answers():
    session = _session(
        (200, envelope({**META_BOX, "type": "SOMETHING_NEW"})),
        (404, None),
        (200, not_found()),
    )
    assert await BudbeeApiClient(session).async_get_parcel(CODE) is None


async def test_delivery_without_a_conspectus_warns_and_keeps_the_payload(caplog):
    session = _session(
        (200, envelope(META_DELIVERY)),
        (200, envelope({"somethingElse": {"state": "NotStarted"}})),
    )

    parcel = await BudbeeApiClient(session).async_get_parcel(CODE)

    assert parcel["somethingElse"] == {"state": "NotStarted"}
    assert "conspectus" in caplog.text
    # keys only, never values — this payload carries an address
    assert "issues/new" in caplog.text


async def test_hollow_success_envelope_is_treated_as_unknown():
    client = BudbeeApiClient(_session((200, envelope(None))))
    assert await client.async_get_parcel(CODE) is None


async def test_raises_on_error_status():
    client = BudbeeApiClient(_session((500, {})))
    with pytest.raises(BudbeeApiError):
        await client.async_get_parcel(CODE)


async def test_raises_on_unparseable_body():
    client = BudbeeApiClient(_session((200, "not json")))
    with pytest.raises(BudbeeApiError):
        await client.async_get_parcel(CODE)


async def test_raises_on_non_object_envelope():
    client = BudbeeApiClient(_session((200, ["not", "a", "dict"])))
    with pytest.raises(BudbeeApiError):
        await client.async_get_parcel(CODE)


async def test_raises_on_non_object_box_body():
    client = BudbeeApiClient(
        _session((200, envelope(META_BOX)), (200, ["not", "a", "dict"]))
    )
    with pytest.raises(BudbeeApiError):
        await client.async_get_parcel(CODE)


async def test_raises_on_unknown_error_code():
    client = BudbeeApiClient(
        _session((200, {**not_found(), "errorCode": "RATE_LIMITED"}))
    )
    with pytest.raises(BudbeeApiError) as err:
        await client.async_get_parcel(CODE)
    assert "RATE_LIMITED" in str(err.value)


async def test_raises_on_failed_envelope_without_an_error_code():
    client = BudbeeApiClient(
        _session((200, {"status": "FAILED", "errorCode": None, "errorMsg": "boom"}))
    )
    with pytest.raises(BudbeeApiError) as err:
        await client.async_get_parcel(CODE)
    assert "boom" in str(err.value)


async def test_raises_on_failed_envelope_without_any_detail():
    client = BudbeeApiClient(_session((200, {"status": "FAILED"})))
    with pytest.raises(BudbeeApiError):
        await client.async_get_parcel(CODE)


async def test_propagates_network_error():
    """ClientError is left alone — DataUpdateCoordinator already wraps it."""
    session = MagicMock()
    session.get = MagicMock(side_effect=aiohttp.ClientError("boom"))
    with pytest.raises(aiohttp.ClientError):
        await BudbeeApiClient(session).async_get_parcel(CODE)

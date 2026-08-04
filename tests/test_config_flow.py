"""Tests for the Budbee config and options flow."""
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.budbee.config_flow import (
    normalize_tracking_code,
    valid_tracking_code,
)
from custom_components.budbee.const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_PARCELS,
    CONF_REFRESH_INTERVAL,
    CONF_TRACKING_CODE,
    DOMAIN,
)


def test_normalize_tracking_code_strips_and_uppercases():
    assert normalize_tracking_code("budbee 123-456") == "BUDBEE123456"
    assert normalize_tracking_code("") == ""
    assert normalize_tracking_code(None) == ""


def test_valid_tracking_code_bounds():
    """Deliberately wide: one 12-character sample is not a format."""
    assert valid_tracking_code("BUDBEE123456")
    assert not valid_tracking_code("ABC")  # too short
    assert not valid_tracking_code("A" * 41)  # too long


async def test_user_flow_creates_hub_without_input(hass):
    """No account, no postcode — the entry is created straight away."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] == "create_entry"
    assert result["title"] == "Budbee"
    assert result["options"][CONF_PARCELS] == []


async def test_second_hub_rejected(hass):
    MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] == "abort"
    # single_config_entry in the manifest aborts before the flow runs.
    assert result["reason"] == "single_instance_allowed"


def _hub(parcels: list[dict]) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        options={CONF_PARCELS: parcels},
    )


def _init_input(
    *, add="", remove=None,
    interval="30",
    filter_type="days", amount=7,
) -> dict:
    """Build the sectioned options-form submission."""
    parcels: dict = {"add": add}
    if remove is not None:
        parcels["remove"] = remove
    return {
        "parcels": parcels,
        "delivered": {
            CONF_DELIVERED_FILTER_TYPE: filter_type,
            CONF_DELIVERED_FILTER_AMOUNT: amount,
        },
        "polling": {CONF_REFRESH_INTERVAL: interval},
    }


async def test_options_add_parcel(hass):
    entry = _hub([])
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _init_input(add="budbee123456")
    )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_PARCELS] == [
        {CONF_TRACKING_CODE: "BUDBEE123456"}
    ]


async def test_options_add_code_with_separators(hass):
    """Pasted codes with spaces/dashes are sanitised like the consumer site."""
    entry = _hub([])
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _init_input(add="budbee-123 456")
    )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_PARCELS] == [
        {CONF_TRACKING_CODE: "BUDBEE123456"}
    ]


async def test_options_add_invalid_tracking_code(hass):
    entry = _hub([])
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _init_input(add="abc")
    )
    assert result["errors"]["base"] == "invalid_tracking_code"


async def test_options_add_duplicate_rejected(hass):
    entry = _hub([{CONF_TRACKING_CODE: "BUDBEE111111"}])
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _init_input(add="BUDBEE111111", remove=[])
    )
    assert result["errors"]["base"] == "already_tracked"


async def test_options_remove_parcel(hass):
    entry = _hub([
        {CONF_TRACKING_CODE: "BUDBEE111111"},
        {CONF_TRACKING_CODE: "BUDBEE222222"},
    ])
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _init_input(remove=["BUDBEE111111"])
    )
    assert result["type"] == "create_entry"
    codes = {p[CONF_TRACKING_CODE] for p in result["data"][CONF_PARCELS]}
    assert codes == {"BUDBEE222222"}


async def test_options_remove_then_readd_same_code(hass):
    """Remove-then-add order: re-adding a just-removed code works."""
    entry = _hub([{CONF_TRACKING_CODE: "BUDBEE111111"}])
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _init_input(add="BUDBEE111111", remove=["BUDBEE111111"])
    )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_PARCELS] == [{CONF_TRACKING_CODE: "BUDBEE111111"}]


async def test_options_changes_interval_and_delivered(hass):
    entry = _hub([])
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        _init_input(interval="120", filter_type="parcels", amount=5),
    )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_REFRESH_INTERVAL] == 120
    assert result["data"][CONF_DELIVERED_FILTER_TYPE] == "parcels"
    assert result["data"][CONF_DELIVERED_FILTER_AMOUNT] == 5

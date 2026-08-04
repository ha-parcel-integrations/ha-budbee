"""Device triggers for the Budbee parcel tracker integration.

Surfaces the parcel events the coordinator fires on the HA event bus as
no-code automation triggers. Each trigger filters on the hub's ``device_id``
(attached to every event).
"""
from __future__ import annotations

import voluptuous as vol
from homeassistant.components.device_automation import DEVICE_TRIGGER_BASE_SCHEMA
from homeassistant.components.homeassistant.triggers import event as event_trigger
from homeassistant.const import (
    CONF_DEVICE_ID,
    CONF_DOMAIN,
    CONF_PLATFORM,
    CONF_TYPE,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers.trigger import TriggerActionType, TriggerInfo
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN

# Device-trigger type -> bus event fired by the coordinator. Incoming parcels
# get registered/status_changed/delivered/delivery_time_changed; outgoing ones
# (a return, an on-demand pickup) get status_changed/delivered only — no
# "registered" and no delivery-time trigger, same as the rest of the suite.
#
# On a locker order "delivered" is Budbee's ``PickedUp``, i.e. the user has the
# parcel in hand. Budbee's own ``Delivered`` means it reached the locker and
# fires a status change to ``at_pickup_point``.
TRIGGER_EVENTS = {
    "parcel_registered": f"{DOMAIN}_parcel_registered",
    "parcel_status_changed": f"{DOMAIN}_parcel_status_changed",
    "parcel_delivered": f"{DOMAIN}_parcel_delivered",
    "parcel_delivery_time_changed": f"{DOMAIN}_parcel_delivery_time_changed",
    "outgoing_parcel_status_changed": f"{DOMAIN}_outgoing_parcel_status_changed",
    "outgoing_parcel_delivered": f"{DOMAIN}_outgoing_parcel_delivered",
}
TRIGGER_TYPES = set(TRIGGER_EVENTS)

TRIGGER_SCHEMA = DEVICE_TRIGGER_BASE_SCHEMA.extend(
    {vol.Required(CONF_TYPE): vol.In(TRIGGER_TYPES)}
)


async def async_get_triggers(
    hass: HomeAssistant, device_id: str
) -> list[dict[str, str]]:
    """Return the list of parcel triggers for a Budbee device."""
    return [
        {
            CONF_PLATFORM: "device",
            CONF_DOMAIN: DOMAIN,
            CONF_DEVICE_ID: device_id,
            CONF_TYPE: trigger_type,
        }
        for trigger_type in TRIGGER_TYPES
    ]


async def async_attach_trigger(
    hass: HomeAssistant,
    config: ConfigType,
    action: TriggerActionType,
    trigger_info: TriggerInfo,
) -> CALLBACK_TYPE:
    """Attach a device trigger by delegating to the event trigger."""
    event_config = event_trigger.TRIGGER_SCHEMA(
        {
            event_trigger.CONF_PLATFORM: "event",
            event_trigger.CONF_EVENT_TYPE: TRIGGER_EVENTS[config[CONF_TYPE]],
            event_trigger.CONF_EVENT_DATA: {CONF_DEVICE_ID: config[CONF_DEVICE_ID]},
        }
    )
    return await event_trigger.async_attach_trigger(
        hass, event_config, action, trigger_info, platform_type="device"
    )

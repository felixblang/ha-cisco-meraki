"""Event platform for the Cisco Meraki integration: MT sensor button presses."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from homeassistant.components.event import (
    EventDeviceClass,
    EventEntity,
)
from homeassistant.core import callback

from .const import LOGGER
from .entity import MerakiEntity

if TYPE_CHECKING:
    from homeassistant.components.mqtt import ReceiveMessage
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import MerakiConfigEntry, MerakiDataUpdateCoordinator

PARALLEL_UPDATES = 0

# meraki/v1/mt/<network ID>/ble/<sensor MAC>/buttonPressed
BUTTON_TOPIC = "meraki/v1/mt/+/ble/+/buttonPressed"

# Sensor models with a button, and the presses they tell apart. MT14 and MT15 only
# report that their button was pressed; the MT30 also reports short and long presses.
BUTTON_MODELS: dict[str, list[str]] = {
    "MT14": ["press"],
    "MT15": ["press"],
    "MT30": ["press", "short_press", "long_press"],
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MerakiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up a button event per MT sensor with a button, if MQTT is set up."""
    # Presses only reach Home Assistant in time over MQTT; through the API they would
    # arrive minutes late.
    if "mqtt" not in hass.config.components:
        return
    coordinator = entry.runtime_data
    buttons = {
        device.mac.lower(): MerakiButtonEvent(coordinator, serial, event_types)
        for serial, device in coordinator.data.devices.items()
        if device.mac
        and (event_types := BUTTON_MODELS.get((device.model or "")[:4])) is not None
    }
    if not buttons:
        return
    async_add_entities(buttons.values())
    entry.async_create_background_task(
        hass, _async_subscribe(hass, entry, buttons), f"{entry.entry_id} buttons"
    )


async def _async_subscribe(
    hass: HomeAssistant,
    entry: MerakiConfigEntry,
    buttons: dict[str, MerakiButtonEvent],
) -> None:
    # Imported here: the MQTT integration and its client library are optional.
    from homeassistant.components import mqtt  # noqa: PLC0415

    if not await mqtt.async_wait_for_mqtt_client(hass):
        return

    @callback
    def _async_message(msg: ReceiveMessage) -> None:
        # A retained message is a press from the past, not one happening now.
        if msg.retain:
            return
        mac = msg.topic.split("/")[-2].lower()
        if (button := buttons.get(mac)) is not None:
            button.async_pressed(msg.payload)

    entry.async_on_unload(
        await mqtt.async_subscribe(hass, BUTTON_TOPIC, _async_message)
    )
    LOGGER.debug("Receiving MT button presses from MQTT topic %s", BUTTON_TOPIC)


class MerakiButtonEvent(MerakiEntity, EventEntity):
    """The button of an MT sensor."""

    _attr_device_class = EventDeviceClass.BUTTON
    _attr_translation_key = "button"

    def __init__(
        self,
        coordinator: MerakiDataUpdateCoordinator,
        serial: str,
        event_types: list[str],
    ) -> None:
        """Initialize the event entity."""
        super().__init__(coordinator, serial, "button")
        self._attr_event_types = event_types

    @callback
    def async_pressed(self, payload: Any) -> None:
        """Fire the event for a buttonPressed message."""
        try:
            data = json.loads(payload)
        except ValueError:
            return
        if not isinstance(data, dict) or data.get("buttonPressed") is False:
            return
        # The MT30 tells short and long presses apart; MT14 and MT15 do not.
        press_type = data.get("pressType") or data.get("type")
        event_type = (
            f"{press_type}_press"
            if f"{press_type}_press" in self.event_types
            else "press"
        )
        self._trigger_event(event_type, {"pressed_at": data.get("ts")})
        self.async_write_ha_state()

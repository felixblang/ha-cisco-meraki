"""Binary sensor platform for the Cisco Meraki integration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)

from .entity import MerakiPortEntity, async_setup_port_entities

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import MerakiConfigEntry, MerakiDataUpdateCoordinator

# The coordinator polls; entities never request their own updates.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001
    entry: MerakiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up a link sensor per switch port."""
    async_setup_port_entities(entry, async_add_entities, MerakiPortLinkSensor)


class MerakiPortLinkSensor(MerakiPortEntity, BinarySensorEntity):
    """Reports whether something is connected to a switch port."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_translation_key = "port_link"

    def __init__(
        self, coordinator: MerakiDataUpdateCoordinator, serial: str, port_id: str
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, serial, port_id, "link")

    @property
    def is_on(self) -> bool | None:
        """Return True if the port has a link."""
        if self.port.status is None:
            return None
        return self.port.status == "Connected"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the link details and what is connected to the port."""
        port = self.port
        return {
            "connector": port.connector,
            "speed": port.speed,
            "duplex": port.duplex,
            "poe": port.poe_active,
            "neighbor": port.neighbor,
            "clients": port.clients,
        }

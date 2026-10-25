"""Binary sensor platform for the Cisco Meraki integration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import callback

from .entity import MerakiEntity, MerakiPortEntity, async_setup_port_entities

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
    """Set up a link sensor per switch port and a lockout sensor per MT40."""
    async_setup_port_entities(entry, async_add_entities, MerakiPortLinkSensor)

    coordinator = entry.runtime_data
    known_mt40s: set[str] = set()

    @callback
    def _async_add_new_mt40s() -> None:
        if new := coordinator.data.power_controls.keys() - known_mt40s:
            known_mt40s.update(new)
            async_add_entities(
                MerakiRemoteLockoutSensor(coordinator, serial) for serial in sorted(new)
            )

    _async_add_new_mt40s()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_new_mt40s))


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


class MerakiRemoteLockoutSensor(MerakiEntity, BinarySensorEntity):
    """The lockout switch on an MT40; while locked, it ignores remote commands."""

    # On means unlocked: remote power commands work.
    _attr_device_class = BinarySensorDeviceClass.LOCK
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "remote_lockout"

    def __init__(self, coordinator: MerakiDataUpdateCoordinator, serial: str) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, serial, "remote_lockout")

    @property
    def is_on(self) -> bool | None:
        """Return True if the lockout switch is unlocked."""
        locked = self.coordinator.data.power_controls[self._serial].locked
        return None if locked is None else not locked

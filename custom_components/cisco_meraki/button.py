"""Button platform for the Cisco Meraki integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.const import Platform
from homeassistant.core import callback

from .entity import MerakiEntity, async_remove_unwritable

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import MerakiConfigEntry, MerakiDataUpdateCoordinator

# Power commands go through the Dashboard API; send them one at a time.
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MerakiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up a power cycle button per MT40 the API key may control."""
    coordinator = entry.runtime_data
    async_remove_unwritable(hass, entry, Platform.BUTTON)
    known: set[str] = set()

    @callback
    def _async_add_new_mt40s() -> None:
        current = {
            serial
            for serial in coordinator.data.power_controls
            if coordinator.can_write(coordinator.data.devices[serial].network_id)
        }
        if new := current - known:
            known.update(new)
            async_add_entities(
                MerakiPowerCycleButton(coordinator, serial) for serial in sorted(new)
            )

    _async_add_new_mt40s()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_new_mt40s))


class MerakiPowerCycleButton(MerakiEntity, ButtonEntity):
    """Turns the device plugged into an MT40 off and on again."""

    _attr_translation_key = "power_cycle"
    _attr_device_class = ButtonDeviceClass.RESTART

    def __init__(self, coordinator: MerakiDataUpdateCoordinator, serial: str) -> None:
        """Initialize the button."""
        super().__init__(coordinator, serial, "power_cycle")

    async def async_press(self) -> None:
        """Turn the power off and on again."""
        await self.coordinator.async_power_command(self._serial, "cycleDownstreamPower")

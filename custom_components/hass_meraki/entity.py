"""Base entity for the Meraki Dashboard integration."""

from __future__ import annotations

from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import MerakiDataUpdateCoordinator, MerakiDevice


class MerakiEntity(CoordinatorEntity[MerakiDataUpdateCoordinator]):
    """An entity belonging to a single Meraki device."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: MerakiDataUpdateCoordinator, serial: str, key: str
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._serial = serial
        self._attr_unique_id = f"{serial}_{key}"

        device = self.device
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
            connections={(CONNECTION_NETWORK_MAC, device.mac)} if device.mac else set(),
            manufacturer=MANUFACTURER,
            model=device.model,
            name=device.name,
            serial_number=serial,
            sw_version=device.firmware,
            configuration_url=device.url,
        )

    @property
    def device(self) -> MerakiDevice:
        """Return the latest data for this entity's device."""
        return self.coordinator.data.devices[self._serial]

    @property
    def available(self) -> bool:
        """Return False if the device disappeared from the organization."""
        return super().available and self._serial in self.coordinator.data.devices

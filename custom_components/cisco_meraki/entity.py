"""Base entity for the Cisco Meraki integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.core import callback
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import MerakiDataUpdateCoordinator

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.helpers.entity import Entity
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import MerakiConfigEntry, MerakiDevice, MerakiSwitchPort


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


class MerakiPortEntity(MerakiEntity):
    """An entity for one port of a Meraki switch."""

    def __init__(
        self,
        coordinator: MerakiDataUpdateCoordinator,
        serial: str,
        port_id: str,
        key: str,
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator, serial, f"port_{port_id}_{key}")
        self._port_id = port_id
        self._attr_translation_placeholders = {"port": port_id}

    @property
    def port(self) -> MerakiSwitchPort:
        """Return the latest data for this entity's port."""
        return self.coordinator.data.ports[self._serial][self._port_id]

    @property
    def available(self) -> bool:
        """Return False if the port disappeared from the switch."""
        return super().available and self._port_id in self.coordinator.data.ports.get(
            self._serial, {}
        )


@callback
def async_setup_port_entities(
    entry: MerakiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    entity_factory: Callable[[MerakiDataUpdateCoordinator, str, str], Entity],
    supported: Callable[[MerakiDataUpdateCoordinator, str, str], bool] | None = None,
) -> None:
    """Add an entity per switch port now and for ports that appear later."""
    coordinator = entry.runtime_data
    known: set[tuple[str, str]] = set()

    @callback
    def _async_add_new_ports() -> None:
        current = {
            (serial, port_id)
            for serial, ports in coordinator.data.ports.items()
            for port_id in ports
            if supported is None or supported(coordinator, serial, port_id)
        }
        if new := current - known:
            known.update(new)
            async_add_entities(
                entity_factory(coordinator, serial, port_id)
                for serial, port_id in sorted(new)
            )

    _async_add_new_ports()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_new_ports))

"""Device trackers for the clients of Meraki networks."""

from __future__ import annotations

from typing import TYPE_CHECKING, override

from homeassistant.components.device_tracker import ScannerEntity
from homeassistant.const import Platform
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import MerakiDataUpdateCoordinator

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import MerakiConfigEntry

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MerakiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up trackers for the clients of the organization's networks."""
    coordinator = entry.runtime_data
    org_id = coordinator.organization_id

    # The API forgets clients after an hour; their trackers stay, as away.
    registry_entries = [
        registry_entry
        for registry_entry in er.async_entries_for_config_entry(
            er.async_get(hass), entry.entry_id
        )
        if registry_entry.domain == Platform.DEVICE_TRACKER
    ]
    tracked = {registry_entry.unique_id for registry_entry in registry_entries}
    async_add_entities(
        MerakiClientTracker(
            coordinator,
            registry_entry.unique_id.removeprefix(f"{org_id}_"),
            registry_entry.original_name,
        )
        for registry_entry in registry_entries
    )

    @callback
    def _async_add_new_clients() -> None:
        new: list[MerakiClientTracker] = []
        for mac, client in coordinator.data.clients.items():
            if (unique_id := _unique_id(org_id, mac)) not in tracked:
                tracked.add(unique_id)
                new.append(MerakiClientTracker(coordinator, mac, client.description))
        async_add_entities(new)

    _async_add_new_clients()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_new_clients))


def _unique_id(org_id: str, mac: str) -> str:
    # The same device can be a client in several organizations.
    return f"{org_id}_{mac}"


class MerakiClientTracker(
    CoordinatorEntity[MerakiDataUpdateCoordinator], ScannerEntity
):
    """A Wi-Fi or wired client, home while it is connected to the network."""

    def __init__(
        self, coordinator: MerakiDataUpdateCoordinator, mac: str, name: str | None
    ) -> None:
        """Initialize the tracker."""
        super().__init__(coordinator)
        self.mac = mac
        self._attr_mac_address = mac
        self._attr_name = name or mac
        self._async_update_attrs()

    @property
    @override
    def unique_id(self) -> str:
        """Return the MAC, scoped to the organization."""
        return _unique_id(self.coordinator.organization_id, self.mac)

    @property
    @override
    def is_connected(self) -> bool:
        """Return True if Meraki reports the client as online."""
        client = self.coordinator.data.clients.get(self.mac)
        return client is not None and client.online

    @callback
    @override
    def _handle_coordinator_update(self) -> None:
        self._async_update_attrs()
        super()._handle_coordinator_update()

    @callback
    def _async_update_attrs(self) -> None:
        if client := self.coordinator.data.clients.get(self.mac):
            self._attr_hostname = client.description
            self._attr_ip_address = client.ip

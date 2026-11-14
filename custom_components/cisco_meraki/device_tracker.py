"""Device trackers for the clients of Meraki networks."""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, Any, override

from homeassistant.components.device_tracker import ScannerEntity
from homeassistant.const import Platform
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from meraki import APIError

from .const import CONF_CLIENT_NETWORKS, CONF_CLIENT_VLANS, LOGGER
from .coordinator import MerakiDataUpdateCoordinator

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import MerakiClient, MerakiConfigEntry

PARALLEL_UPDATES = 0


def client_filter(
    options: Mapping[str, Any],
) -> Callable[[MerakiClient], bool] | None:
    """Return whether a client gets a tracker, or None if every client does."""
    networks = set(options.get(CONF_CLIENT_NETWORKS) or ())
    vlans = {str(vlan) for vlan in options.get(CONF_CLIENT_VLANS) or ()}
    if not networks and not vlans:
        return None

    def _matches(client: MerakiClient) -> bool:
        return (not networks or client.network_id in networks) and (
            not vlans or client.vlan in vlans
        )

    return _matches


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MerakiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up trackers for the clients of the organization's networks."""
    coordinator = entry.runtime_data
    org_id = coordinator.organization_id
    matches = client_filter(entry.options)

    # The API forgets clients after an hour; their trackers stay, as away.
    registry_entries = [
        registry_entry
        for registry_entry in er.async_entries_for_config_entry(
            er.async_get(hass), entry.entry_id
        )
        if registry_entry.domain == Platform.DEVICE_TRACKER
    ]
    if matches:
        # Trackers of current clients outside the filter go right away.
        kept = [
            registry_entry
            for registry_entry in registry_entries
            if not _async_remove_if_excluded(
                hass, entry, registry_entry, coordinator.data.clients, matches
            )
        ]
        LOGGER.debug(
            "Client filter: %s of %s trackers removed; current clients by VLAN: %s",
            len(registry_entries) - len(kept),
            len(registry_entries),
            dict(Counter(c.vlan for c in coordinator.data.clients.values())),
        )
        registry_entries = kept
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
            if matches and not matches(client):
                continue
            if (unique_id := _unique_id(org_id, mac)) not in tracked:
                tracked.add(unique_id)
                new.append(MerakiClientTracker(coordinator, mac, client.description))
        async_add_entities(new)

    _async_add_new_clients()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_new_clients))

    if matches and (
        unlisted := [
            registry_entry
            for registry_entry in registry_entries
            if _mac(org_id, registry_entry) not in coordinator.data.clients
        ]
    ):
        # Looking up a month of clients takes a while, so it does not delay startup.
        entry.async_create_background_task(
            hass,
            _async_remove_unlisted_excluded(
                hass, entry, unlisted, matches, tracked.discard
            ),
            f"{entry.entry_id} client filter",
        )


async def _async_remove_unlisted_excluded(
    hass: HomeAssistant,
    entry: MerakiConfigEntry,
    registry_entries: list[er.RegistryEntry],
    matches: Callable[[MerakiClient], bool],
    untrack: Callable[[str], None],
) -> None:
    """Remove trackers of earlier clients that were last seen outside the filter."""
    try:
        history = await entry.runtime_data.async_fetch_client_history()
    except APIError as err:
        LOGGER.debug("Could not look up earlier clients: %s", err)
        return
    removed = 0
    for registry_entry in registry_entries:
        if _async_remove_if_excluded(hass, entry, registry_entry, history, matches):
            untrack(registry_entry.unique_id)
            removed += 1
    org_id = entry.runtime_data.organization_id
    LOGGER.debug(
        "Client filter: %s of %s trackers of earlier clients removed, "
        "%s not seen in the last month; clients of the month by VLAN: %s",
        removed,
        len(registry_entries),
        sum(1 for e in registry_entries if _mac(org_id, e) not in history),
        dict(Counter(c.vlan for c in history.values())),
    )


@callback
def _async_remove_if_excluded(
    hass: HomeAssistant,
    entry: MerakiConfigEntry,
    registry_entry: er.RegistryEntry,
    clients: Mapping[str, MerakiClient],
    matches: Callable[[MerakiClient], bool],
) -> bool:
    """Remove a tracker and its device if its client is known to be outside."""
    client = clients.get(_mac(entry.runtime_data.organization_id, registry_entry))
    # A client that has not been seen for a month keeps its tracker.
    if client is None or matches(client):
        return False

    entity_registry = er.async_get(hass)
    entity_registry.async_remove(registry_entry.entity_id)
    device_registry = dr.async_get(hass)
    if (
        (device_id := registry_entry.device_id) is not None
        and (device := device_registry.async_get(device_id)) is not None
        # Only the device created for the tracker goes, not another integration's
        # device that the tracker was added to.
        and device.config_entry_id == entry.entry_id
        and not any(device.identifiers)
        and not er.async_entries_for_device(
            entity_registry, device_id, include_disabled_entities=True
        )
    ):
        device_registry.async_remove_device(device_id)
    return True


def _mac(org_id: str, registry_entry: er.RegistryEntry) -> str:
    return registry_entry.unique_id.removeprefix(f"{org_id}_")


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
        # Each client gets a device of its own. Home Assistant 2026.10 ignores this
        # and handles the device itself; from 2026.11 on, trackers must provide it.
        self._attr_device_info = dr.DeviceInfo(
            connections={(dr.CONNECTION_NETWORK_MAC, mac)}, name=name or mac
        )
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

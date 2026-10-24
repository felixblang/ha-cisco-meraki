"""Switch platform for the Cisco Meraki integration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory, Platform
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er

from .entity import (
    MerakiEntity,
    MerakiPortEntity,
    MerakiSsidEntity,
    async_setup_port_entities,
    async_setup_ssid_entities,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import MerakiConfigEntry, MerakiDataUpdateCoordinator

# Port and SSID changes go through the Dashboard API; send them one at a time.
PARALLEL_UPDATES = 1

# Disabling an uplink or stacking port cuts the switch off from the network or breaks
# the stack, so those entities start disabled and have to be enabled deliberately.
PROTECTED_PORT_TYPES = frozenset({"stack", "svl", "dad"})


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MerakiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up a switch per switch port and SSID the API key may change."""
    coordinator = entry.runtime_data

    # Switches that a read-only key could not use are left out, and removed if the
    # key lost its write access since they were added.
    registry = er.async_get(hass)
    for registry_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if registry_entry.domain == Platform.SWITCH and not coordinator.can_write(
            _network_of(coordinator, registry_entry.unique_id)
        ):
            registry.async_remove(registry_entry.entity_id)

    def _port_writable(
        coordinator: MerakiDataUpdateCoordinator,
        serial: str,
        port_id: str,  # noqa: ARG001
    ) -> bool:
        return coordinator.can_write(coordinator.data.devices[serial].network_id)

    async_setup_port_entities(
        entry, async_add_entities, MerakiPortSwitch, _port_writable
    )
    async_setup_ssid_entities(
        entry,
        async_add_entities,
        MerakiSsidSwitch,
        lambda coordinator, network_id: coordinator.can_write(network_id),
    )

    known_tags: set[tuple[str, str]] = set()

    @callback
    def _async_add_new_tags() -> None:
        # Only tags that SSIDs are limited to: tags also select group policies,
        # alerts and more, which Home Assistant should not change by accident.
        current = {
            (serial, tag)
            for serial, device in coordinator.data.devices.items()
            if device.product_type == "wireless"
            and coordinator.can_write(device.network_id)
            for tag in coordinator.ssid_tags(device.network_id)
        }
        if new := current - known_tags:
            known_tags.update(new)
            async_add_entities(
                MerakiTagSwitch(coordinator, serial, tag) for serial, tag in sorted(new)
            )

    _async_add_new_tags()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_new_tags))

    known_cameras: set[str] = set()

    @callback
    def _async_add_new_cameras() -> None:
        current = {
            serial
            for serial in coordinator.data.camera_video
            if coordinator.can_write(coordinator.data.devices[serial].network_id)
        }
        if new := current - known_cameras:
            known_cameras.update(new)
            async_add_entities(
                MerakiRtspSwitch(coordinator, serial) for serial in sorted(new)
            )

    _async_add_new_cameras()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_new_cameras))


def _network_of(coordinator: MerakiDataUpdateCoordinator, unique_id: str) -> str | None:
    """Return the network of a switch entity by its unique ID."""
    if "_ssid_" in unique_id:
        return unique_id.split("_ssid_", maxsplit=1)[0]
    # Device entities start with the serial, which has no underscores.
    device = coordinator.data.devices.get(unique_id.split("_", maxsplit=1)[0])
    return device.network_id if device else None


class MerakiPortSwitch(MerakiPortEntity, SwitchEntity):
    """Enables or disables a switch port."""

    _attr_translation_key = "port"

    def __init__(
        self, coordinator: MerakiDataUpdateCoordinator, serial: str, port_id: str
    ) -> None:
        """Initialize the switch."""
        super().__init__(coordinator, serial, port_id, "enabled")
        port = self.port
        self._attr_entity_registry_enabled_default = not (
            port.is_uplink or port.type in PROTECTED_PORT_TYPES
        )

    @property
    def is_on(self) -> bool:
        """Return True if the port is enabled."""
        return self.port.enabled

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the port's Dashboard configuration."""
        port = self.port
        return {
            "description": port.name,
            "type": port.type,
            "vlan": port.vlan,
            "uplink": port.is_uplink,
            "neighbor": port.neighbor,
            "clients": port.clients,
        }

    async def async_turn_on(self, **kwargs: Any) -> None:  # noqa: ARG002
        """Enable the port."""
        await self.coordinator.async_set_port_enabled(
            self._serial, self._port_id, enabled=True
        )

    async def async_turn_off(self, **kwargs: Any) -> None:  # noqa: ARG002
        """Disable the port."""
        await self.coordinator.async_set_port_enabled(
            self._serial, self._port_id, enabled=False
        )


class MerakiSsidSwitch(MerakiSsidEntity, SwitchEntity):
    """Enables or disables an SSID."""

    _attr_translation_key = "ssid"

    def __init__(
        self, coordinator: MerakiDataUpdateCoordinator, network_id: str, number: int
    ) -> None:
        """Initialize the switch."""
        super().__init__(coordinator, network_id, number, "enabled")

    @property
    def is_on(self) -> bool:
        """Return True if the SSID is enabled."""
        return self.ssid.enabled

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the SSID's Dashboard configuration."""
        ssid = self.ssid
        return {
            "number": ssid.number,
            "auth_mode": ssid.auth_mode,
            "hidden": ssid.visible is False,
            "available_on_all_aps": ssid.available_on_all_aps,
            "availability_tags": list(ssid.availability_tags),
            "access_points": sorted(
                device.name for device in self.coordinator.ssid_access_points(ssid)
            ),
        }

    async def async_turn_on(self, **kwargs: Any) -> None:  # noqa: ARG002
        """Enable the SSID."""
        await self.coordinator.async_set_ssid_enabled(*self._ssid_key, enabled=True)

    async def async_turn_off(self, **kwargs: Any) -> None:  # noqa: ARG002
        """Disable the SSID."""
        await self.coordinator.async_set_ssid_enabled(*self._ssid_key, enabled=False)


class MerakiTagSwitch(MerakiEntity, SwitchEntity):
    """Adds a tag that SSIDs are limited to to an access point, or removes it."""

    _attr_translation_key = "tag"

    def __init__(
        self, coordinator: MerakiDataUpdateCoordinator, serial: str, tag: str
    ) -> None:
        """Initialize the switch."""
        super().__init__(coordinator, serial, f"tag_{tag}")
        self._tag = tag
        self._attr_translation_placeholders = {"tag": tag}

    @property
    def available(self) -> bool:
        """Return False once no SSID is limited to the tag anymore."""
        return super().available and self._tag in self.coordinator.ssid_tags(
            self.device.network_id
        )

    @property
    def is_on(self) -> bool:
        """Return True if the access point has the tag."""
        return self._tag in self.device.tags

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the SSIDs the access point broadcasts with the tag."""
        ssids = self.coordinator.ssid_tags(self.device.network_id).get(self._tag, [])
        return {"ssids": sorted(ssid.name for ssid in ssids)}

    async def async_turn_on(self, **kwargs: Any) -> None:  # noqa: ARG002
        """Add the tag."""
        await self.coordinator.async_set_device_tag(
            self._serial, self._tag, present=True
        )

    async def async_turn_off(self, **kwargs: Any) -> None:  # noqa: ARG002
        """Remove the tag."""
        await self.coordinator.async_set_device_tag(
            self._serial, self._tag, present=False
        )


class MerakiRtspSwitch(MerakiEntity, SwitchEntity):
    """Exposes a camera's RTSP stream on the LAN, which needs no credentials."""

    _attr_translation_key = "rtsp"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: MerakiDataUpdateCoordinator, serial: str) -> None:
        """Initialize the switch."""
        super().__init__(coordinator, serial, "rtsp")

    @property
    def is_on(self) -> bool:
        """Return True if the RTSP stream is exposed."""
        return self.coordinator.data.camera_video[self._serial].rtsp_enabled

    async def async_turn_on(self, **kwargs: Any) -> None:  # noqa: ARG002
        """Expose the RTSP stream."""
        await self.coordinator.async_set_camera_rtsp(self._serial, enabled=True)

    async def async_turn_off(self, **kwargs: Any) -> None:  # noqa: ARG002
        """Stop exposing the RTSP stream."""
        await self.coordinator.async_set_camera_rtsp(self._serial, enabled=False)

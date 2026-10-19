"""Switch platform for the Cisco Meraki integration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.switch import SwitchEntity

from .entity import MerakiPortEntity, async_setup_port_entities

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import MerakiConfigEntry, MerakiDataUpdateCoordinator

# Port changes go through the Dashboard API; send them one at a time.
PARALLEL_UPDATES = 1

# Disabling an uplink or stacking port cuts the switch off from the network or breaks
# the stack, so those entities start disabled and have to be enabled deliberately.
PROTECTED_PORT_TYPES = frozenset({"stack", "svl", "dad"})


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001
    entry: MerakiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up a switch per switch port."""
    async_setup_port_entities(entry, async_add_entities, MerakiPortSwitch)


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

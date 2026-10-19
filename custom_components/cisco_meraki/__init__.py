"""The Cisco Meraki integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.const import CONF_API_KEY, Platform

from . import mqtt_source
from .api import async_close_client, async_create_client
from .coordinator import MerakiConfigEntry, MerakiDataUpdateCoordinator
from .sensor_coordinator import MerakiSensorCoordinator

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR, Platform.SWITCH]


async def async_setup_entry(hass: HomeAssistant, entry: MerakiConfigEntry) -> bool:
    """Set up Cisco Meraki from a config entry."""
    client = await async_create_client(hass, entry.data[CONF_API_KEY])
    entry.async_on_unload(lambda: async_close_client(client))

    coordinator = MerakiDataUpdateCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    if coordinator.sensors_present:
        coordinator.sensors = MerakiSensorCoordinator(hass, entry, client, coordinator)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    if coordinator.sensors:
        # The first fetch fills the gap since the last run, up to a day of readings,
        # so it runs in the background instead of delaying startup.
        entry.async_create_background_task(
            hass,
            _async_start_sensors(hass, entry, coordinator.sensors),
            f"{entry.entry_id} sensors",
        )
    return True


async def _async_start_sensors(
    hass: HomeAssistant, entry: MerakiConfigEntry, sensors: MerakiSensorCoordinator
) -> None:
    """Backfill sensor readings, then take new ones from MQTT if it is available."""
    await sensors.async_refresh()
    # Subscribing only now keeps MQTT readings from getting ahead of the backfill.
    if unsubscribe := await mqtt_source.async_subscribe(hass, sensors):
        entry.async_on_unload(unsubscribe)


async def async_unload_entry(hass: HomeAssistant, entry: MerakiConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(hass: HomeAssistant, entry: MerakiConfigEntry) -> None:
    """Reload the entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)

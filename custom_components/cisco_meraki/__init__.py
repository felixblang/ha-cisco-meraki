"""The Cisco Meraki integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.const import CONF_API_KEY, EVENT_HOMEASSISTANT_STOP, Platform

from . import camera_sense, mqtt_source
from .api import async_close_client, async_create_client
from .coordinator import MerakiConfigEntry, MerakiDataUpdateCoordinator
from .sensor_coordinator import MerakiSensorCoordinator

if TYPE_CHECKING:
    from homeassistant.core import Event, HomeAssistant
    from meraki.aio import AsyncDashboardAPI

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.CAMERA,
    Platform.DEVICE_TRACKER,
    Platform.SENSOR,
    Platform.SWITCH,
]


async def async_setup_entry(hass: HomeAssistant, entry: MerakiConfigEntry) -> bool:
    """Set up Cisco Meraki from a config entry."""
    client = await async_create_client(hass, entry.data[CONF_API_KEY])
    entry.async_on_unload(lambda: async_close_client(client))

    # Config entries are not unloaded when Home Assistant stops.
    async def _async_close(_: Event) -> None:
        await async_close_client(client)

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_close)
    )

    coordinator = MerakiDataUpdateCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    # Checked at every setup, so a changed administrator access applies on reload.
    await coordinator.async_load_write_access()
    entry.runtime_data = coordinator
    if coordinator.sensors_present:
        coordinator.sensors = MerakiSensorCoordinator(hass, entry, client, coordinator)
    if cameras := [
        serial
        for serial, device in coordinator.data.devices.items()
        if device.product_type == "camera"
    ]:
        coordinator.camera_sense = camera_sense.MerakiCameraSense(cameras)

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
    if coordinator.camera_sense:
        entry.async_create_background_task(
            hass,
            _async_start_camera_sense(hass, entry, client, coordinator.camera_sense),
            f"{entry.entry_id} camera sense",
        )
    return True


async def _async_start_camera_sense(
    hass: HomeAssistant,
    entry: MerakiConfigEntry,
    client: AsyncDashboardAPI,
    sense: camera_sense.MerakiCameraSense,
) -> None:
    """Name the cameras' zones, poll them once, then follow MQTT."""
    await sense.async_load_zone_names(client)
    # Without this, cameras that only the API reaches would wait for the next poll.
    await sense.async_poll(client)
    if unsubscribe := await camera_sense.async_subscribe(hass, sense):
        entry.async_on_unload(unsubscribe)


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

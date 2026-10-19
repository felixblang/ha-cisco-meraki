"""Tests for the Cisco Meraki setup and sensors."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.cisco_meraki.const import DEFAULT_SCAN_INTERVAL, DOMAIN

from .conftest import DEVICES, make_api_error


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_sensors(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Sensors and devices are created from the organization's devices."""
    await _setup(hass, mock_config_entry)
    assert mock_config_entry.state is ConfigEntryState.LOADED

    assert hass.states.get("sensor.office_ap_status").state == "online"
    assert hass.states.get("sensor.office_ap_clients").state == "2"
    # Unnamed devices fall back to their model; cameras get no client sensor.
    assert hass.states.get("sensor.mv12_status").state == "offline"
    assert hass.states.get("sensor.mv12_clients") is None

    device = device_registry.async_get_device_by_identifier(
        (DOMAIN, "Q2AA-AAAA-AAAA"), mock_config_entry.entry_id
    )
    assert device is not None
    assert device.model == "MR36"
    assert device.sw_version == "wireless-31-1-6"
    assert device.configuration_url == "https://n1.meraki.com/office-ap"

    # Only the network with a client-serving device is queried for clients.
    mock_client.networks.getNetworkClients.assert_awaited_once()


async def test_new_device_added(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Devices that appear after setup get entities on the next poll."""
    await _setup(hass, mock_config_entry)
    mock_client.organizations.getOrganizationDevices.return_value = [
        *DEVICES,
        {"serial": "Q2CC-CCCC-CCCC", "name": "Core", "productType": "switch"},
    ]
    freezer.tick(DEFAULT_SCAN_INTERVAL + timedelta(seconds=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)

    # No availability entry for the new switch yet, so its status is unknown.
    assert hass.states.get("sensor.core_status").state == "unknown"
    assert hass.states.get("sensor.core_clients").state == "0"


async def test_update_failure_marks_unavailable(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A failed poll makes the entities unavailable until the next success."""
    await _setup(hass, mock_config_entry)
    mock_client.organizations.getOrganizationDevices.side_effect = make_api_error(503)
    freezer.tick(DEFAULT_SCAN_INTERVAL + timedelta(seconds=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get("sensor.office_ap_status").state == "unavailable"


async def test_setup_auth_failure_starts_reauth(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """An invalid key during setup triggers a reauth flow."""
    mock_client.organizations.getOrganizationDevices.side_effect = make_api_error(401)
    await _setup(hass, mock_config_entry)
    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]


async def test_unload(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """Unloading closes the API client."""
    await _setup(hass, mock_config_entry)
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.NOT_LOADED
    mock_client.__aexit__.assert_awaited()

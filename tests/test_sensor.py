"""Tests for the Cisco Meraki setup and sensors."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.cisco_meraki.const import DEFAULT_SCAN_INTERVAL, DOMAIN

from .conftest import DEVICES, QUALITY_START, UPLINK_STATUSES, make_api_error


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


async def test_client_closed_on_stop(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """Home Assistant stopping closes the API client."""
    await _setup(hass, mock_config_entry)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    mock_client.__aexit__.assert_awaited_once()


async def test_all_requests_finish_before_failing(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """The client is only closed once every request of a failed update finished."""
    devices_running = False

    async def _get_devices(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        nonlocal devices_running
        devices_running = True
        # Slower than the error takes to reach config entry setup.
        for _ in range(100):
            await asyncio.sleep(0)
        devices_running = False
        return [dict(d) for d in DEVICES]

    mock_client.organizations.getOrganizationDevices.side_effect = _get_devices
    mock_client.organizations.getOrganizationDevicesAvailabilities.side_effect = (
        make_api_error(503)
    )
    running_when_closed: list[bool] = []
    mock_client.__aexit__.side_effect = lambda *_: running_when_closed.append(
        devices_running
    )
    await _setup(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY
    assert running_when_closed == [False]


async def test_uplinks(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Each WAN uplink gets a status sensor that follows failovers."""
    await _setup(hass, mock_config_entry)

    wan1 = hass.states.get("sensor.gateway_wan_1")
    assert wan1.state == "active"
    assert wan1.attributes["public_ip"] == "198.51.100.7"
    assert wan1.attributes["ip_assigned_by"] == "dhcp"
    assert wan1.attributes["options"] == [
        "active",
        "ready",
        "connecting",
        "not_connected",
        "failed",
    ]
    # The API spells it "not connected".
    assert hass.states.get("sensor.gateway_wan_2").state == "not_connected"
    assert hass.states.get("sensor.gateway_cellular").state == "ready"

    # WAN 1 fails and the appliance fails over to cellular.
    uplinks = deepcopy(UPLINK_STATUSES)
    uplinks[0]["uplinks"][0]["status"] = "failed"
    uplinks[0]["uplinks"][2]["status"] = "active"
    mock_client.organizations.getOrganizationUplinksStatuses.return_value = uplinks
    freezer.tick(DEFAULT_SCAN_INTERVAL + timedelta(seconds=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert hass.states.get("sensor.gateway_wan_1").state == "failed"
    assert hass.states.get("sensor.gateway_cellular").state == "active"


async def test_no_appliances_skips_uplink_requests(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """Organizations without appliances make no uplink requests."""
    mock_client.organizations.getOrganizationDevices.return_value = [
        d for d in DEVICES if d["productType"] != "appliance"
    ]
    await _setup(hass, mock_config_entry)
    mock_client.organizations.getOrganizationUplinksStatuses.assert_not_awaited()


async def test_uplink_loss_and_latency(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Uplinks report loss and latency, with statistics at the measurement times."""
    now = QUALITY_START + timedelta(minutes=10)
    freezer.move_to(now)
    await _setup(hass, mock_config_entry)
    await async_wait_recording_done(hass)

    # The IPv4 destination is used, not the IPv6 one with 99 ms.
    latency = hass.states.get("sensor.gateway_wan_1_latency")
    assert float(latency.state) == 32
    assert latency.attributes["unit_of_measurement"] == "ms"
    assert latency.attributes["destination"] == "8.8.8.8"
    assert (
        latency.attributes["measured_at"]
        == (QUALITY_START + timedelta(minutes=6)).isoformat()
    )
    assert float(hass.states.get("sensor.gateway_wan_1_packet_loss").state) == 0
    # A dead uplink has no latency, only loss.
    assert hass.states.get("sensor.gateway_wan_2_latency").state == "unknown"
    assert float(hass.states.get("sensor.gateway_wan_2_packet_loss").state) == 100

    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        QUALITY_START - timedelta(hours=1),
        None,
        {"sensor.gateway_wan_1_latency"},
        "5minute",
        None,
        {"mean", "min", "max"},
    )
    row = stats["sensor.gateway_wan_1_latency"][0]
    assert dt_util.utc_from_timestamp(row["start"]) == QUALITY_START.replace(second=0)
    assert (row["mean"], row["min"], row["max"]) == (24, 20, 28)

    # Each request covers at most 5 minutes and ends at least 2 minutes ago.
    get_quality = mock_client.organizations.getOrganizationDevicesUplinksLossAndLatency
    get_quality.reset_mock()
    freezer.tick(DEFAULT_SCAN_INTERVAL + timedelta(seconds=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    for call in get_quality.call_args_list:
        t0 = dt_util.parse_datetime(call.kwargs["t0"])
        t1 = dt_util.parse_datetime(call.kwargs["t1"])
        assert timedelta(0) < t1 - t0 <= timedelta(minutes=5)
        assert t1 <= dt_util.utcnow() - timedelta(minutes=2)
    assert get_quality.await_count >= 1


async def test_uplink_quality_failure_is_not_fatal(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """Without loss and latency, the uplink status still works."""
    get_quality = mock_client.organizations.getOrganizationDevicesUplinksLossAndLatency
    get_quality.side_effect = make_api_error(400)
    await _setup(hass, mock_config_entry)
    assert hass.states.get("sensor.gateway_wan_1").state == "active"
    assert hass.states.get("sensor.gateway_wan_1_latency") is None

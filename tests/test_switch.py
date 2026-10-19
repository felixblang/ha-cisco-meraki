"""Tests for the Cisco Meraki switch port entities."""

from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.components.switch import (
    DOMAIN as SWITCH_DOMAIN,
)
from homeassistant.components.switch import (
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
)
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.cisco_meraki.const import (
    DEFAULT_SCAN_INTERVAL,
    PORT_CHANGE_REFRESH_DELAYS,
)

from .conftest import SWITCH_PORT_STATUSES, USAGE_START, make_api_error

SWITCH_SERIAL = "Q2SW-SSSS-SSSS"


@pytest.fixture
async def setup_entry(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> MockConfigEntry:
    """Set up the integration."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    return mock_config_entry


@pytest.mark.usefixtures("setup_entry")
async def test_port_entities(
    hass: HomeAssistant, entity_registry: er.EntityRegistry
) -> None:
    """Every port gets a switch and a link sensor on the switch's device."""
    port1 = hass.states.get("switch.core_switch_port_1")
    assert port1.state == "on"
    assert port1.attributes["description"] == "Desk"
    assert port1.attributes["vlan"] == 1
    assert hass.states.get("switch.core_switch_port_2").state == "off"

    assert hass.states.get("binary_sensor.core_switch_port_1_link").state == "on"
    link2 = hass.states.get("binary_sensor.core_switch_port_2_link")
    assert link2.state == "off"
    assert link2.attributes["speed"] is None

    # The uplink can cut the switch off, so its switch starts disabled.
    uplink = entity_registry.async_get("switch.core_switch_port_8")
    assert uplink.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert hass.states.get("binary_sensor.core_switch_port_8_link").state == "on"

    # Ports of switches outside the organization's devices are ignored.
    assert not [
        entry
        for entry in entity_registry.entities.values()
        if entry.unique_id.startswith("Q2XX")
    ]


@pytest.mark.usefixtures("setup_entry")
async def test_toggle_port(hass: HomeAssistant, mock_client: MagicMock) -> None:
    """Turning a port switch on or off updates the port in the Dashboard."""
    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TURN_OFF,
        {ATTR_ENTITY_ID: "switch.core_switch_port_1"},
        blocking=True,
    )
    mock_client.switch.updateDeviceSwitchPort.assert_awaited_once_with(
        SWITCH_SERIAL, "1", enabled=False
    )
    assert hass.states.get("switch.core_switch_port_1").state == "off"

    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: "switch.core_switch_port_2"},
        blocking=True,
    )
    mock_client.switch.updateDeviceSwitchPort.assert_awaited_with(
        SWITCH_SERIAL, "2", enabled=True
    )
    assert hass.states.get("switch.core_switch_port_2").state == "on"


@pytest.mark.parametrize(
    ("status", "translation_key"),
    [(403, "write_not_permitted"), (400, "port_update_failed")],
)
@pytest.mark.usefixtures("setup_entry")
async def test_toggle_port_error(
    hass: HomeAssistant, mock_client: MagicMock, status: int, translation_key: str
) -> None:
    """A rejected change raises and leaves the state untouched."""
    mock_client.switch.updateDeviceSwitchPort.side_effect = make_api_error(status)
    with pytest.raises(HomeAssistantError) as exc_info:
        await hass.services.async_call(
            SWITCH_DOMAIN,
            SERVICE_TURN_OFF,
            {ATTR_ENTITY_ID: "switch.core_switch_port_1"},
            blocking=True,
        )
    assert exc_info.value.translation_key == translation_key
    assert hass.states.get("switch.core_switch_port_1").state == "on"


async def test_no_switches_skips_port_requests(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """Organizations without switches make no switch port requests."""
    mock_client.organizations.getOrganizationDevices.return_value = [
        d
        for d in mock_client.organizations.getOrganizationDevices.return_value
        if d["productType"] != "switch"
    ]
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    mock_client.switch.getOrganizationSwitchPortsBySwitch.assert_not_awaited()


@pytest.mark.usefixtures("setup_entry")
async def test_port_neighbors_and_clients(hass: HomeAssistant) -> None:
    """Ports show their LLDP neighbor and the wired clients seen on them."""
    port1 = hass.states.get("switch.core_switch_port_1")
    assert port1.attributes["neighbor"] is None
    # Clients without a description fall back to their MAC address.
    assert port1.attributes["clients"] == ["aa:bb:cc:dd:ee:ff", "office-pc"]

    uplink_link = hass.states.get("binary_sensor.core_switch_port_8_link")
    assert uplink_link.attributes["neighbor"] == "Router"
    assert uplink_link.attributes["clients"] == []

    # Without a system name, the chassis ID identifies the neighbor.
    port2 = hass.states.get("switch.core_switch_port_2")
    assert port2.attributes["neighbor"] == "bb:bb:bb:bb:bb:bb"


async def test_topology_failure_is_not_fatal(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """Missing neighbor data does not stop the ports from loading."""
    topology = mock_client.switch.getOrganizationSwitchPortsTopologyDiscoveryByDevice
    topology.side_effect = make_api_error(400)
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    port8 = hass.states.get("binary_sensor.core_switch_port_8_link")
    assert port8.state == "on"
    assert port8.attributes["neighbor"] is None


@pytest.mark.usefixtures("setup_entry")
async def test_refresh_after_toggle(
    hass: HomeAssistant, mock_client: MagicMock, freezer: FrozenDateTimeFactory
) -> None:
    """The link state is polled again shortly after a port change."""
    get_statuses = mock_client.switch.getOrganizationSwitchPortsStatusesBySwitch
    assert get_statuses.await_count == 1

    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TURN_OFF,
        {ATTR_ENTITY_ID: "switch.core_switch_port_1"},
        blocking=True,
    )
    # The switch applies the change and the port goes down.
    statuses = deepcopy(SWITCH_PORT_STATUSES)
    statuses["items"][0]["ports"][0]["status"] = "Disabled"
    get_statuses.return_value = statuses

    elapsed = 0
    for refreshes, delay in enumerate(PORT_CHANGE_REFRESH_DELAYS, start=2):
        freezer.tick(timedelta(seconds=delay - elapsed + 1))
        elapsed = delay + 1
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert get_statuses.await_count == refreshes

    assert hass.states.get("binary_sensor.core_switch_port_1_link").state == "off"


async def test_port_connector(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Ports report RJ45 or SFP, looked up once per switch."""
    get_ports = mock_client.switch.getDeviceSwitchPorts
    get_ports.side_effect = make_api_error(500)
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    # A failed lookup leaves the connector unknown but does not fail the update.
    link1 = hass.states.get("binary_sensor.core_switch_port_1_link")
    assert link1.state == "on"
    assert link1.attributes["connector"] is None

    get_ports.side_effect = None
    for _ in range(2):
        freezer.tick(DEFAULT_SCAN_INTERVAL + timedelta(seconds=1))
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=True)

    link = "binary_sensor.core_switch_port_{}_link"
    assert hass.states.get(link.format(1)).attributes["connector"] == "RJ45"
    assert hass.states.get(link.format(8)).attributes["connector"] == "SFP"
    # Retried after the failure, then cached.
    assert get_ports.await_count == 2


@pytest.mark.usefixtures("mock_client")
async def test_port_poe(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """PoE ports report power and energy, even without a network link."""
    freezer.move_to(USAGE_START + timedelta(minutes=80))
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)

    link = hass.states.get("binary_sensor.core_switch_port_2_link")
    assert link.state == "off"
    assert link.attributes["poe"] is True

    # 0.1 Wh per 5 minutes is 1.2 W.
    assert float(hass.states.get("sensor.core_switch_port_2_poe_power").state) == (
        pytest.approx(1.2)
    )
    energy = hass.states.get("sensor.core_switch_port_2_poe_energy")
    assert float(energy.state) == pytest.approx(0.0014)
    statistic = "cisco_meraki:q2sw_ssss_ssss_port_2_poe_energy"
    assert energy.attributes["statistic_id"] == statistic

    # Ports that never delivered PoE power get no PoE sensors.
    for port in (1, 8):
        assert hass.states.get(f"sensor.core_switch_port_{port}_poe_power") is None
        assert hass.states.get(f"sensor.core_switch_port_{port}_poe_energy") is None

    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        USAGE_START,
        None,
        {statistic},
        "hour",
        None,
        {"sum"},
    )
    # The 09:00 hour holds the twelve intervals that started in it.
    assert [row["sum"] for row in stats[statistic]] == pytest.approx([0.0012])

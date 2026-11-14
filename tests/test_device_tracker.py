"""Tests for the client device trackers."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import STATE_HOME, STATE_NOT_HOME, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from custom_components.cisco_meraki.const import (
    CONF_CLIENT_NETWORKS,
    CONF_CLIENT_VLANS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)

from .conftest import DEVICES, ORG_ID, make_api_error

PHONE = "device_tracker.pixel_9"
PHONE_MAC = "aa:bb:cc:10:00:01"

# As getNetworkClients returns them, trimmed to the fields the trackers use.
CLIENTS = [
    {
        "mac": PHONE_MAC,
        "description": "Pixel 9",
        "ip": "192.168.1.20",
        "lastSeen": "2026-10-20T20:59:00Z",
        "recentDeviceSerial": "Q2AA-AAAA-AAAA",
        "status": "Online",
    },
    {
        "mac": "aa:bb:cc:10:00:02",
        "description": "NAS",
        "ip": "192.168.1.30",
        "lastSeen": "2026-10-20T20:59:30Z",
        "recentDeviceSerial": "Q2SW-SSSS-SSSS",
        "switchport": "4",
        "status": "Online",
    },
    {
        "mac": "aa:bb:cc:10:00:03",
        "description": None,
        "ip": "192.168.1.40",
        "lastSeen": "2026-10-20T20:15:00Z",
        "recentDeviceSerial": "Q2AA-AAAA-AAAA",
        "status": "Offline",
    },
]


@pytest.fixture
def clients(mock_client: MagicMock) -> list[dict[str, Any]]:
    """Return the clients the API lists; change them before the next poll."""
    clients = [dict(client) for client in CLIENTS]
    mock_client.networks.getNetworkClients.return_value = clients
    return clients


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    if hass.config_entries.async_get_entry(entry.entry_id) is None:
        entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


async def _poll(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    freezer.tick(DEFAULT_SCAN_INTERVAL)
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


@pytest.mark.usefixtures("entity_registry_enabled_by_default", "clients", "freezer")
async def test_trackers(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    """Clients are home while Meraki reports them online."""
    await _setup(hass, mock_config_entry)

    phone = hass.states.get(PHONE)
    assert phone.state == STATE_HOME
    assert phone.attributes["ip"] == "192.168.1.20"
    assert phone.attributes["mac"] == PHONE_MAC
    assert phone.attributes["host_name"] == "Pixel 9"
    assert hass.states.get("device_tracker.nas").state == STATE_HOME
    # Clients without a name are named by their MAC address.
    assert hass.states.get("device_tracker.aa_bb_cc_10_00_03").state == STATE_NOT_HOME
    assert entity_registry.async_get(PHONE).unique_id == f"{ORG_ID}_{PHONE_MAC}"


@pytest.mark.usefixtures("clients", "freezer")
async def test_trackers_disabled_by_default(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Only clients known to another integration are tracked by default."""
    other_entry = MockConfigEntry(domain="other")
    other_entry.add_to_hass(hass)
    device_registry.async_get_or_create(
        config_entry_id=other_entry.entry_id,
        connections={(dr.CONNECTION_NETWORK_MAC, PHONE_MAC)},
    )
    await _setup(hass, mock_config_entry)

    assert hass.states.get(PHONE).state == STATE_HOME
    nas = entity_registry.async_get("device_tracker.nas")
    assert nas.disabled_by is er.RegistryEntryDisabler.INTEGRATION


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_client_leaves(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    clients: list[dict[str, Any]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Clients are away when offline or no longer listed."""
    await _setup(hass, mock_config_entry)
    assert hass.states.get(PHONE).state == STATE_HOME

    clients[0]["status"] = "Offline"
    await _poll(hass, freezer)
    assert hass.states.get(PHONE).state == STATE_NOT_HOME

    # After an hour without connecting, the API stops listing a client.
    del clients[0]
    await _poll(hass, freezer)
    assert hass.states.get(PHONE).state == STATE_NOT_HOME


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_new_client(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    clients: list[dict[str, Any]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Clients that connect later get a tracker."""
    await _setup(hass, mock_config_entry)
    clients.append({**CLIENTS[0], "mac": "aa:bb:cc:10:00:04", "description": "Tablet"})
    await _poll(hass, freezer)
    assert hass.states.get("device_tracker.tablet").state == STATE_HOME


@pytest.mark.usefixtures("entity_registry_enabled_by_default", "freezer")
async def test_restore_unlisted_clients(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    clients: list[dict[str, Any]],
) -> None:
    """Trackers of clients the API no longer lists are away after a reload."""
    await _setup(hass, mock_config_entry)
    clients.clear()
    await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert hass.states.get(PHONE).state == STATE_NOT_HOME
    assert hass.states.get("device_tracker.nas").state == STATE_NOT_HOME


@pytest.mark.usefixtures("entity_registry_enabled_by_default", "freezer")
async def test_client_in_several_networks(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A client listed in two networks is home where it is online."""
    mock_client.organizations.getOrganizationDevices.return_value = [
        *(dict(d) for d in DEVICES),
        {
            "serial": "Q2CC-CCCC-CCCC",
            "name": "Lab AP",
            "model": "MR28",
            "productType": "wireless",
            "networkId": "N_2",
        },
    ]
    # Meraki documents lastSeen as a Unix timestamp; the other clients have the
    # ISO strings the API actually returns. 1792528200 is 2026-10-20 20:30 UTC.
    elsewhere = [
        {**client, "status": "Offline", "ip": "10.0.0.1", "lastSeen": 1792528200}
        for client in CLIENTS
    ]
    mock_client.networks.getNetworkClients.side_effect = lambda network_id, **_: (
        [dict(c) for c in CLIENTS] if network_id == "N_1" else elsewhere
    )
    await _setup(hass, mock_config_entry)

    phone = hass.states.get(PHONE)
    assert phone.state == STATE_HOME
    assert phone.attributes["ip"] == "192.168.1.20"
    # Offline in both, the more recent sighting (20:30 rather than 20:15) wins.
    laptop = hass.states.get("device_tracker.aa_bb_cc_10_00_03")
    assert laptop.attributes["ip"] == "10.0.0.1"


@pytest.mark.usefixtures("entity_registry_enabled_by_default", "clients")
async def test_update_failed(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Trackers become unavailable when the API fails."""
    await _setup(hass, mock_config_entry)
    mock_client.networks.getNetworkClients.side_effect = make_api_error(503)
    await _poll(hass, freezer)
    assert hass.states.get(PHONE).state == STATE_UNAVAILABLE


def _set_options(
    hass: HomeAssistant, entry: MockConfigEntry, options: dict[str, Any]
) -> None:
    if hass.config_entries.async_get_entry(entry.entry_id) is None:
        entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, options=options)


def _vlans(clients: list[dict[str, Any]], *vlans: Any) -> None:
    for client, vlan in zip(clients, vlans, strict=True):
        client["vlan"] = vlan


@pytest.mark.usefixtures("entity_registry_enabled_by_default", "freezer")
async def test_vlan_filter(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    clients: list[dict[str, Any]],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Only clients in the selected VLANs get a tracker."""
    # The API reports the VLAN as a number or a string.
    _vlans(clients, 1, "11", 20)
    _set_options(hass, mock_config_entry, {CONF_CLIENT_VLANS: ["1", "11"]})
    await _setup(hass, mock_config_entry)

    assert hass.states.get(PHONE).state == STATE_HOME
    assert hass.states.get("device_tracker.nas").state == STATE_HOME
    assert hass.states.get("device_tracker.aa_bb_cc_10_00_03") is None

    # Clients without a VLAN are outside a VLAN selection.
    clients.append({**CLIENTS[0], "mac": "aa:bb:cc:10:00:04", "description": "Tablet"})
    await _poll(hass, freezer)
    assert hass.states.get("device_tracker.tablet") is None


@pytest.mark.usefixtures("entity_registry_enabled_by_default", "freezer")
async def test_network_filter(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Only clients in the selected networks get a tracker."""
    mock_client.organizations.getOrganizationDevices.return_value = [
        *(dict(d) for d in DEVICES),
        {
            "serial": "Q2CC-CCCC-CCCC",
            "name": "Lab AP",
            "model": "MR28",
            "productType": "wireless",
            "networkId": "N_2",
        },
    ]
    lab = {**CLIENTS[0], "mac": "aa:bb:cc:20:00:01", "description": "Lab laptop"}
    mock_client.networks.getNetworkClients.side_effect = lambda network_id, **_: (
        [dict(c) for c in CLIENTS] if network_id == "N_1" else [dict(lab)]
    )
    _set_options(hass, mock_config_entry, {CONF_CLIENT_NETWORKS: ["N_2"]})
    await _setup(hass, mock_config_entry)

    assert hass.states.get("device_tracker.lab_laptop").state == STATE_HOME
    assert hass.states.get(PHONE) is None


@pytest.mark.usefixtures("entity_registry_enabled_by_default", "freezer")
async def test_filter_removes_trackers(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    clients: list[dict[str, Any]],
    entity_registry: er.EntityRegistry,
) -> None:
    """A filter removes the trackers of clients outside it and their devices."""
    _vlans(clients, 1, 20, 20)
    device_registry = dr.async_get(hass)
    other_entry = MockConfigEntry(domain="other")
    other_entry.add_to_hass(hass)
    nas_mac = CLIENTS[1]["mac"]
    device_registry.async_get_or_create(
        config_entry_id=other_entry.entry_id,
        connections={(dr.CONNECTION_NETWORK_MAC, nas_mac)},
    )
    # Devices of the integration's own, as Home Assistant creates for trackers.
    mock_config_entry.add_to_hass(hass)
    for mac in ("aa:bb:cc:10:00:03", "aa:bb:cc:10:00:04"):
        device_registry.async_get_or_create(
            config_entry_id=mock_config_entry.entry_id,
            connections={(dr.CONNECTION_NETWORK_MAC, mac)},
        )
    # A fourth client was seen earlier in the month in VLAN 20, a fifth in VLAN 1,
    # and a sixth not at all; their trackers are left from before.
    earlier = [
        {**CLIENTS[2], "mac": "aa:bb:cc:10:00:04", "vlan": 20, "status": "Offline"},
        {**CLIENTS[2], "mac": "aa:bb:cc:10:00:05", "vlan": 1, "status": "Offline"},
    ]
    await _setup(hass, mock_config_entry)
    clients.extend(
        dict(c) for c in [*earlier, {**CLIENTS[2], "mac": "aa:bb:cc:10:00:06"}]
    )
    await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    del clients[3:]
    trackers = {
        e.unique_id: e
        for e in er.async_entries_for_config_entry(
            entity_registry, mock_config_entry.entry_id
        )
        if e.domain == "device_tracker"
    }
    assert len(trackers) == 6

    month = [dict(c) for c in [*clients, *earlier]]
    mock_client.networks.getNetworkClients.side_effect = lambda _, timespan, **__: (
        month if timespan > 3600 else [dict(c) for c in clients]
    )
    hass.config_entries.async_update_entry(
        mock_config_entry, options={CONF_CLIENT_VLANS: ["1"]}
    )
    await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    def kept(mac: str) -> bool:
        return (
            entity_registry.async_get_entity_id(
                "device_tracker", "cisco_meraki", f"{ORG_ID}_{mac}"
            )
            is not None
        )

    assert kept(PHONE_MAC)
    assert not kept(nas_mac)
    assert not kept("aa:bb:cc:10:00:03")
    assert not kept("aa:bb:cc:10:00:04")
    assert kept("aa:bb:cc:10:00:05")
    # Not seen for a month, so its VLAN is unknown.
    assert kept("aa:bb:cc:10:00:06")
    assert hass.states.get("device_tracker.nas") is None

    for mac in ("aa:bb:cc:10:00:03", "aa:bb:cc:10:00:04"):
        device_id = trackers[f"{ORG_ID}_{mac}"].device_id
        assert device_registry.async_get(device_id) is None
    # Another integration's device stays, without the Meraki entry.
    nas = device_registry.async_get_device_by_connection(
        (dr.CONNECTION_NETWORK_MAC, nas_mac), other_entry.entry_id
    )
    assert nas.config_entry_id == other_entry.entry_id


@pytest.mark.usefixtures("entity_registry_enabled_by_default", "clients", "freezer")
async def test_remove_client_device(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    mock_config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Client devices can be removed, Meraki devices cannot."""
    assert await async_setup_component(hass, "config", {})
    mock_config_entry.add_to_hass(hass)
    device_registry.async_get_or_create(
        config_entry_id=mock_config_entry.entry_id,
        connections={(dr.CONNECTION_NETWORK_MAC, PHONE_MAC)},
    )
    await _setup(hass, mock_config_entry)
    client = await hass_ws_client(hass)

    async def remove(device_id: str) -> bool:
        await client.send_json_auto_id(
            {
                "type": "config/device_registry/remove_config_entry",
                "config_entry_id": mock_config_entry.entry_id,
                "device_id": device_id,
            }
        )
        return (await client.receive_json())["success"]

    assert await remove(entity_registry.async_get(PHONE).device_id)
    await hass.async_block_till_done()
    assert entity_registry.async_get(PHONE) is None

    ap = device_registry.async_get_device_by_identifier(
        (DOMAIN, "Q2AA-AAAA-AAAA"), mock_config_entry.entry_id
    )
    assert not await remove(ap.id)

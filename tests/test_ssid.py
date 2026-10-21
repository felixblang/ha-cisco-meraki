"""Tests for SSIDs and for leaving out switches a read-only API key cannot use."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.switch import (
    DOMAIN as SWITCH_DOMAIN,
)
from homeassistant.components.switch import (
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
)
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.cisco_meraki.const import DEFAULT_SCAN_INTERVAL, DOMAIN

from .conftest import (
    ADMIN_EMAIL,
    CLIENTS,
    DEVICES,
    NETWORK_ID,
    SSIDS,
    make_api_error,
)

HOME_SSID = "switch.home_wi_fi_home"
GUEST_SSID = "switch.home_wi_fi_guests"
HOME_CLIENTS = "sensor.home_wi_fi_home_clients"
PORT_SWITCH = "switch.core_switch_port_1"


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


async def _poll(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    freezer.tick(DEFAULT_SCAN_INTERVAL)
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


def _set_admin(mock_client: MagicMock, **access: Any) -> None:
    mock_client.organizations.getOrganizationAdmins.return_value = [
        {"email": ADMIN_EMAIL, "orgAccess": "none", "networks": [], "tags": []} | access
    ]


def _set_ssids(mock_client: MagicMock, ssids: list[dict[str, Any]]) -> None:
    mock_client.wireless.getNetworkWirelessSsids.side_effect = lambda _: ssids


@pytest.mark.usefixtures("freezer")
async def test_ssids(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    """Configured SSIDs get a switch and a client count; unconfigured ones nothing."""
    mock_client.networks.getNetworkClients.return_value = [
        *CLIENTS,
        {"mac": "aa:bb:cc:10:00:01", "status": "Online", "ssid": "Home"},
        {"mac": "aa:bb:cc:10:00:02", "status": "Online", "ssid": "Home"},
        {"mac": "aa:bb:cc:10:00:03", "status": "Offline", "ssid": "Home"},
    ]
    await _setup(hass, mock_config_entry)

    home = hass.states.get(HOME_SSID)
    assert home.state == STATE_ON
    assert home.attributes["number"] == 0
    assert home.attributes["auth_mode"] == "psk"
    assert home.attributes["hidden"] is False
    # Configured but disabled SSIDs can be turned on from Home Assistant.
    assert hass.states.get(GUEST_SSID).state == STATE_OFF
    assert hass.states.get(HOME_CLIENTS).state == "2"
    assert hass.states.get("sensor.home_wi_fi_guests_clients").state == "0"
    assert not [s for s in hass.states.async_all() if "unconfigured" in s.entity_id]

    device = device_registry.async_get(entity_registry.async_get(HOME_SSID).device_id)
    assert device.identifiers == {(DOMAIN, f"{NETWORK_ID}_wireless")}
    assert device.name == "Home Wi-Fi"
    assert device.entry_type is dr.DeviceEntryType.SERVICE


@pytest.mark.usefixtures("freezer")
async def test_turn_ssid_on_and_off(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """Turning the switch on or off enables or disables the SSID."""
    await _setup(hass, mock_config_entry)
    update = mock_client.wireless.updateNetworkWirelessSsid

    await hass.services.async_call(
        SWITCH_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: GUEST_SSID}, blocking=True
    )
    update.assert_awaited_once_with(NETWORK_ID, "1", enabled=True)
    assert hass.states.get(GUEST_SSID).state == STATE_ON

    update.reset_mock()
    await hass.services.async_call(
        SWITCH_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: HOME_SSID}, blocking=True
    )
    update.assert_awaited_once_with(NETWORK_ID, "0", enabled=False)
    assert hass.states.get(HOME_SSID).state == STATE_OFF


@pytest.mark.parametrize(
    ("status", "message"),
    [
        pytest.param(403, "read-only", id="not_permitted"),
        pytest.param(400, "Could not change SSID Guests", id="failed"),
    ],
)
@pytest.mark.usefixtures("freezer")
async def test_turn_ssid_on_error(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    status: int,
    message: str,
) -> None:
    """A refused change is reported and the switch stays as it was."""
    await _setup(hass, mock_config_entry)
    mock_client.wireless.updateNetworkWirelessSsid.side_effect = make_api_error(status)

    with pytest.raises(HomeAssistantError, match=message):
        await hass.services.async_call(
            SWITCH_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: GUEST_SSID}, blocking=True
        )
    assert hass.states.get(GUEST_SSID).state == STATE_OFF


async def test_ssid_renamed_and_reset(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A renamed SSID keeps its entity; one reset to unconfigured is unavailable."""
    await _setup(hass, mock_config_entry)
    ssids = [dict(ssid) for ssid in SSIDS]
    ssids[0]["name"] = "Home 2"
    ssids[1] = {**ssids[1], "name": "Unconfigured SSID 2"}
    _set_ssids(mock_client, ssids)
    await _poll(hass, freezer)

    assert hass.states.get(HOME_SSID).attributes["friendly_name"] == "Home Wi-Fi Home 2"
    assert (
        hass.states.get(HOME_CLIENTS).attributes["friendly_name"]
        == "Home Wi-Fi Home 2 clients"
    )
    assert hass.states.get(GUEST_SSID).state == STATE_UNAVAILABLE


@pytest.mark.parametrize(
    "access",
    [
        pytest.param({"orgAccess": "read-only"}, id="read_only"),
        pytest.param(
            {"networks": [{"id": NETWORK_ID, "access": "read-only"}]},
            id="network_read_only",
        ),
        pytest.param(
            {"networks": [{"id": "N_2", "access": "full"}]}, id="other_network"
        ),
        # The key's administrator is not among the organization's admins.
        pytest.param(
            {"email": "someone@example.com", "orgAccess": "full"}, id="other_admin"
        ),
    ],
)
@pytest.mark.usefixtures("freezer")
async def test_read_only_key(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    access: dict[str, Any],
) -> None:
    """Without write access to the network there are no switches, but the sensors."""
    _set_admin(mock_client, **access)
    await _setup(hass, mock_config_entry)

    assert hass.states.get(PORT_SWITCH) is None
    assert hass.states.get(HOME_SSID) is None
    assert hass.states.get(HOME_CLIENTS) is not None


@pytest.mark.parametrize(
    "access",
    [
        pytest.param({"orgAccess": "full"}, id="organization"),
        pytest.param(
            {"networks": [{"id": NETWORK_ID, "access": "full"}]}, id="network"
        ),
        pytest.param({"tags": [{"tag": "home", "access": "full"}]}, id="network_tag"),
    ],
)
@pytest.mark.usefixtures("freezer")
async def test_write_access(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    access: dict[str, Any],
) -> None:
    """Full access to the organization, the network or one of its tags adds switches."""
    mock_client.organizations.getOrganizationNetworks.return_value = [
        {"id": NETWORK_ID, "name": "Home", "tags": ["home"]},
        {"id": "N_2", "name": "Lab", "tags": []},
    ]
    _set_admin(mock_client, **access)
    await _setup(hass, mock_config_entry)

    assert hass.states.get(PORT_SWITCH) is not None
    assert hass.states.get(HOME_SSID) is not None


@pytest.mark.usefixtures("freezer")
async def test_admin_list_refused(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """A key that may not read the admin list is treated as read-only."""
    mock_client.organizations.getOrganizationAdmins.side_effect = make_api_error(403)
    await _setup(hass, mock_config_entry)
    assert hass.states.get(PORT_SWITCH) is None
    assert hass.states.get(HOME_SSID) is None


@pytest.mark.usefixtures("freezer")
async def test_lost_write_access_removes_switches(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    """Switches are removed once the key can no longer use them."""
    await _setup(hass, mock_config_entry)
    assert entity_registry.async_get(PORT_SWITCH) is not None
    assert entity_registry.async_get(HOME_SSID) is not None

    _set_admin(mock_client, orgAccess="read-only")
    await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert entity_registry.async_get(PORT_SWITCH) is None
    assert entity_registry.async_get(HOME_SSID) is None
    assert entity_registry.async_get(HOME_CLIENTS) is not None


AP_SERIAL = "Q2AA-AAAA-AAAA"
TAG_SWITCH = "switch.office_ap_tag_guests"
TAGS_SENSOR = "sensor.office_ap_tags"


@pytest.fixture
def guests_on_tagged_aps(mock_client: MagicMock) -> None:
    """Limit the Guests SSID to access points tagged "guests"; the AP has "office"."""
    mock_client.organizations.getOrganizationDevices.return_value = [
        dict(d) | ({"tags": ["office"]} if d["serial"] == AP_SERIAL else {})
        for d in DEVICES
    ]
    ssids = [dict(ssid) for ssid in SSIDS]
    ssids[1] |= {"availableOnAllAps": False, "availabilityTags": ["guests"]}
    _set_ssids(mock_client, ssids)


@pytest.mark.usefixtures("guests_on_tagged_aps", "freezer")
async def test_ssid_access_points(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """SSIDs show their tags and the access points that broadcast them."""
    await _setup(hass, mock_config_entry)

    home = hass.states.get(HOME_SSID).attributes
    assert home["availability_tags"] == []
    assert home["access_points"] == ["Office AP"]
    guests = hass.states.get(GUEST_SSID).attributes
    assert guests["availability_tags"] == ["guests"]
    # No access point has the tag, so nothing broadcasts the SSID.
    assert guests["access_points"] == []

    assert hass.states.get(TAGS_SENSOR).state == "office"
    tag = hass.states.get(TAG_SWITCH)
    assert tag.state == STATE_OFF
    assert tag.attributes["ssids"] == ["Guests"]
    # Only tags that SSIDs use get a switch.
    assert hass.states.get("switch.office_ap_tag_office") is None


@pytest.mark.usefixtures("guests_on_tagged_aps", "freezer")
async def test_turn_tag_on_and_off(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """The tag switch adds the tag to the access point's tags or removes it."""
    await _setup(hass, mock_config_entry)
    update = mock_client.devices.updateDevice

    await hass.services.async_call(
        SWITCH_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: TAG_SWITCH}, blocking=True
    )
    update.assert_awaited_once_with(AP_SERIAL, tags=["office", "guests"])
    assert hass.states.get(TAG_SWITCH).state == STATE_ON
    assert hass.states.get(GUEST_SSID).attributes["access_points"] == ["Office AP"]

    update.reset_mock()
    await hass.services.async_call(
        SWITCH_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: TAG_SWITCH}, blocking=True
    )
    update.assert_awaited_once_with(AP_SERIAL, tags=["office"])
    assert hass.states.get(TAG_SWITCH).state == STATE_OFF


@pytest.mark.parametrize(
    ("status", "message"),
    [
        pytest.param(403, "read-only", id="not_permitted"),
        pytest.param(400, "Could not change the tags of Office AP", id="failed"),
    ],
)
@pytest.mark.usefixtures("guests_on_tagged_aps", "freezer")
async def test_turn_tag_on_error(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    status: int,
    message: str,
) -> None:
    """A refused tag change is reported and the switch stays as it was."""
    await _setup(hass, mock_config_entry)
    mock_client.devices.updateDevice.side_effect = make_api_error(status)

    with pytest.raises(HomeAssistantError, match=message):
        await hass.services.async_call(
            SWITCH_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: TAG_SWITCH}, blocking=True
        )
    assert hass.states.get(TAG_SWITCH).state == STATE_OFF


@pytest.mark.usefixtures("guests_on_tagged_aps")
async def test_tag_no_longer_used(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A tag switch is unavailable once no SSID is limited to the tag."""
    await _setup(hass, mock_config_entry)
    _set_ssids(mock_client, [dict(ssid) for ssid in SSIDS])
    await _poll(hass, freezer)
    assert hass.states.get(TAG_SWITCH).state == STATE_UNAVAILABLE


@pytest.mark.usefixtures("guests_on_tagged_aps", "freezer")
async def test_read_only_key_no_tag_switches(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """A read-only key sees the tags but gets no tag switches."""
    _set_admin(mock_client, orgAccess="read-only")
    await _setup(hass, mock_config_entry)
    assert hass.states.get(TAGS_SENSOR).state == "office"
    assert hass.states.get(TAG_SWITCH) is None


@pytest.mark.parametrize(
    ("status", "access_points"),
    [
        pytest.param("offline", ["Office AP"], id="offline"),
        pytest.param("dormant", [], id="dormant"),
    ],
)
@pytest.mark.usefixtures("freezer")
async def test_access_point_status(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    status: str,
    access_points: list[str],
) -> None:
    """Offline access points still count, as they get their SSIDs when back."""
    mock_client.organizations.getOrganizationDevicesAvailabilities.return_value = [
        {"serial": AP_SERIAL, "status": status}
    ]
    await _setup(hass, mock_config_entry)
    assert hass.states.get(HOME_SSID).attributes["access_points"] == access_points

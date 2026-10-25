"""Tests for controlling the power of MT40 smart power controllers."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN
from homeassistant.components.button import SERVICE_PRESS
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.components.switch import SERVICE_TURN_OFF, SERVICE_TURN_ON
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.cisco_meraki.const import DEFAULT_SCAN_INTERVAL

from .conftest import ADMIN_EMAIL, DEVICES, NETWORK_ID, make_api_error

MT40 = "Q3CJ-AAAA-AAAA"
POWER = "switch.rack_outlet_power"
CYCLE = "button.rack_outlet_power_cycle"
LOCKOUT = "binary_sensor.rack_outlet_remote_lockout"


def _power_readings(*, enabled: bool, locked: bool) -> list[dict[str, Any]]:
    """Return the MT40 readings as getOrganizationSensorReadingsLatest has them."""
    ts = "2026-10-25T16:26:16.000000Z"
    return [
        {
            "serial": MT40,
            "network": {"id": NETWORK_ID, "name": "Home"},
            "readings": [
                {
                    "ts": ts,
                    "metric": "downstreamPower",
                    "downstreamPower": {"enabled": enabled},
                },
                {
                    "ts": ts,
                    "metric": "remoteLockoutSwitch",
                    "remoteLockoutSwitch": {"locked": locked},
                },
            ],
        }
    ]


@pytest.fixture
def power_readings(mock_client: MagicMock) -> dict[str, bool]:
    """Add an MT40 whose power state can be changed before the next fetch."""
    state = {"enabled": True, "locked": False}
    mock_client.organizations.getOrganizationDevices.return_value = [
        *(dict(d) for d in DEVICES),
        {
            "serial": MT40,
            "name": "Rack outlet",
            "model": "MT40",
            "mac": "aa:bb:cc:00:00:40",
            "productType": "sensor",
            "networkId": NETWORK_ID,
        },
    ]

    def latest(_org: str, **kwargs: Any) -> list[dict[str, Any]]:
        # The sensor readings coordinator asks for all metrics, without a filter.
        if kwargs.get("metrics") is None:
            return []
        return _power_readings(**state)

    mock_client.sensor.getOrganizationSensorReadingsLatest = AsyncMock(
        side_effect=latest
    )
    mock_client.sensor.getOrganizationSensorReadingsHistory = AsyncMock(return_value=[])
    mock_client.sensor.createDeviceSensorCommand = AsyncMock(
        return_value={"commandId": "1", "status": "pending"}
    )
    mock_client.sensor.getDeviceSensorCommand = AsyncMock(
        return_value={"commandId": "1", "status": "completed", "errors": []}
    )
    return state


@pytest.fixture(autouse=True)
def fast_command_polling() -> Any:
    """Do not wait between polls of a command's result."""
    with patch(
        "custom_components.cisco_meraki.coordinator.POWER_COMMAND_POLL_INTERVAL", 0
    ):
        yield


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


@pytest.mark.usefixtures("power_readings", "freezer")
async def test_power_entities(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """An MT40 gets a power switch, a power cycle button and its lockout state."""
    await _setup(hass, mock_config_entry)

    power = hass.states.get(POWER)
    assert power.state == STATE_ON
    assert power.attributes["remote_lockout"] is False
    assert hass.states.get(CYCLE) is not None
    # On means unlocked: remote commands work.
    assert hass.states.get(LOCKOUT).state == STATE_ON


@pytest.mark.parametrize(
    ("call", "operation", "state"),
    [
        pytest.param(
            (SWITCH_DOMAIN, SERVICE_TURN_OFF, POWER),
            "disableDownstreamPower",
            STATE_OFF,
            id="off",
        ),
        pytest.param(
            (SWITCH_DOMAIN, SERVICE_TURN_ON, POWER),
            "enableDownstreamPower",
            STATE_ON,
            id="on",
        ),
        pytest.param(
            (BUTTON_DOMAIN, SERVICE_PRESS, CYCLE),
            "cycleDownstreamPower",
            STATE_ON,
            id="cycle",
        ),
    ],
)
@pytest.mark.usefixtures("power_readings", "freezer")
async def test_power_commands(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    call: tuple[str, str, str],
    operation: str,
    state: str,
) -> None:
    """Commands are sent, and the state only changes once Meraki completed them."""
    await _setup(hass, mock_config_entry)
    domain, service, entity_id = call
    await hass.services.async_call(
        domain, service, {ATTR_ENTITY_ID: entity_id}, blocking=True
    )
    sensor_api = mock_config_entry.runtime_data.client.sensor
    sensor_api.createDeviceSensorCommand.assert_awaited_once_with(MT40, operation)
    sensor_api.getDeviceSensorCommand.assert_awaited_once_with(MT40, "1")
    assert hass.states.get(POWER).state == state


@pytest.mark.usefixtures("freezer")
async def test_locked(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    power_readings: dict[str, bool],
) -> None:
    """A locked MT40 ignores commands, so none are sent."""
    power_readings["locked"] = True
    await _setup(hass, mock_config_entry)
    assert hass.states.get(LOCKOUT).state == STATE_OFF

    with pytest.raises(HomeAssistantError, match="lockout switch on Rack outlet"):
        await hass.services.async_call(
            SWITCH_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: POWER}, blocking=True
        )
    mock_client.sensor.createDeviceSensorCommand.assert_not_awaited()
    assert hass.states.get(POWER).state == STATE_ON


@pytest.mark.parametrize(
    ("setup_mocks", "message"),
    [
        pytest.param(
            {"createDeviceSensorCommand": AsyncMock(side_effect=make_api_error(403))},
            "read-only",
            id="not_permitted",
        ),
        pytest.param(
            {"createDeviceSensorCommand": AsyncMock(side_effect=make_api_error(400))},
            "Could not change the power of Rack outlet",
            id="refused",
        ),
        pytest.param(
            {
                "getDeviceSensorCommand": AsyncMock(
                    return_value={
                        "commandId": "1",
                        "status": "failed",
                        "errors": ["Sensor is offline"],
                    }
                )
            },
            "Sensor is offline",
            id="failed",
        ),
    ],
)
@pytest.mark.usefixtures("power_readings", "freezer")
async def test_power_command_errors(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    setup_mocks: dict[str, AsyncMock],
    message: str,
) -> None:
    """A refused or failed command is reported and the state stays as it was."""
    await _setup(hass, mock_config_entry)
    for name, mock in setup_mocks.items():
        setattr(mock_client.sensor, name, mock)

    with pytest.raises(HomeAssistantError, match=message):
        await hass.services.async_call(
            SWITCH_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: POWER}, blocking=True
        )
    assert hass.states.get(POWER).state == STATE_ON


@pytest.mark.usefixtures("power_readings", "freezer")
async def test_power_command_timeout(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """A command Meraki does not complete in time is reported."""
    await _setup(hass, mock_config_entry)
    mock_client.sensor.getDeviceSensorCommand.return_value = {
        "commandId": "1",
        "status": "pending",
    }
    with (
        patch("custom_components.cisco_meraki.coordinator.POWER_COMMAND_TIMEOUT", 0),
        pytest.raises(HomeAssistantError, match="did not confirm"),
    ):
        await hass.services.async_call(
            SWITCH_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: POWER}, blocking=True
        )
    assert hass.states.get(POWER).state == STATE_ON


async def test_power_state_fetched_every_five_minutes(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    power_readings: dict[str, bool],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Power changed outside Home Assistant shows up within five minutes."""
    await _setup(hass, mock_config_entry)
    power_readings["enabled"] = False

    freezer.tick(DEFAULT_SCAN_INTERVAL)
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(POWER).state == STATE_ON

    freezer.tick(timedelta(minutes=4))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(POWER).state == STATE_OFF


@pytest.mark.usefixtures("power_readings", "freezer")
async def test_read_only_key(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """A read-only key sees the lockout state but gets no power controls."""
    mock_client.organizations.getOrganizationAdmins.return_value = [
        {"email": ADMIN_EMAIL, "orgAccess": "read-only", "networks": [], "tags": []}
    ]
    await _setup(hass, mock_config_entry)
    assert hass.states.get(POWER) is None
    assert hass.states.get(CYCLE) is None
    assert hass.states.get(LOCKOUT) is not None


@pytest.mark.usefixtures("power_readings", "freezer")
async def test_lost_write_access_removes_controls(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    """The power switch and button are removed once the key can no longer use them."""
    await _setup(hass, mock_config_entry)
    assert entity_registry.async_get(CYCLE) is not None

    mock_client.organizations.getOrganizationAdmins.return_value = [
        {"email": ADMIN_EMAIL, "orgAccess": "read-only", "networks": [], "tags": []}
    ]
    await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert entity_registry.async_get(POWER) is None
    assert entity_registry.async_get(CYCLE) is None
    assert entity_registry.async_get(LOCKOUT) is not None

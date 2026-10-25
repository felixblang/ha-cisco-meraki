"""Tests for MT sensor button presses received over MQTT."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from homeassistant.const import STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
)

from .conftest import DEVICES, NETWORK_ID

MT15_MAC = "6C:7F:0C:11:2B:A2"
MT30_MAC = "6C:7F:0C:11:30:30"
MT15_BUTTON = "event.living_room_button"
MT30_BUTTON = "event.desk_button_button"
PRESS = '{"ts":"2026-10-25T17:00:00Z","buttonPressed":true}'


def _topic(mac: str) -> str:
    return f"meraki/v1/mt/{NETWORK_ID}/ble/{mac}/buttonPressed"


@pytest.fixture
def button_sensors(mock_client: MagicMock) -> None:
    """Add an MT15 and an MT30 to the organization."""
    mock_client.organizations.getOrganizationDevices.return_value = [
        *(dict(d) for d in DEVICES),
        {
            "serial": "Q3CQ-AAAA-AAAA",
            "name": "Living room",
            "model": "MT15",
            "mac": MT15_MAC.lower(),
            "productType": "sensor",
            "networkId": NETWORK_ID,
        },
        {
            "serial": "Q3CH-AAAA-AAAA",
            "name": "Desk button",
            "model": "MT30",
            "mac": MT30_MAC.lower(),
            "productType": "sensor",
            "networkId": NETWORK_ID,
        },
    ]


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


@pytest.mark.usefixtures("mqtt_mock", "button_sensors", "freezer")
async def test_press(hass: HomeAssistant, mock_config_entry: MockConfigEntry) -> None:
    """A press published over MQTT fires the button's event."""
    await _setup(hass, mock_config_entry)
    assert hass.states.get(MT15_BUTTON).state == STATE_UNKNOWN
    assert hass.states.get(MT15_BUTTON).attributes["event_types"] == ["press"]

    async_fire_mqtt_message(hass, _topic(MT15_MAC), PRESS)
    await hass.async_block_till_done()

    state = hass.states.get(MT15_BUTTON)
    assert state.state != STATE_UNKNOWN
    assert state.attributes["event_type"] == "press"
    assert state.attributes["pressed_at"] == "2026-10-25T17:00:00Z"


@pytest.mark.parametrize(
    ("payload", "event_type"),
    [
        pytest.param(
            '{"ts":"2026-10-25T17:00:00Z","buttonPressed":true,"pressType":"long"}',
            "long_press",
            id="long",
        ),
        pytest.param(
            '{"ts":"2026-10-25T17:00:00Z","buttonPressed":true,"pressType":"short"}',
            "short_press",
            id="short",
        ),
        pytest.param(PRESS, "press", id="no_type"),
    ],
)
@pytest.mark.usefixtures("mqtt_mock", "button_sensors", "freezer")
async def test_mt30_press_types(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    payload: str,
    event_type: str,
) -> None:
    """The MT30 tells short and long presses apart, if the message says which."""
    await _setup(hass, mock_config_entry)
    async_fire_mqtt_message(hass, _topic(MT30_MAC), payload)
    await hass.async_block_till_done()
    assert hass.states.get(MT30_BUTTON).attributes["event_type"] == event_type


@pytest.mark.parametrize(
    ("topic", "payload", "retain"),
    [
        pytest.param(_topic(MT15_MAC), PRESS, True, id="retained"),
        pytest.param(_topic("6C:7F:0C:99:99:99"), PRESS, False, id="other_sensor"),
        pytest.param(_topic(MT15_MAC), "not json", False, id="invalid"),
        pytest.param(
            _topic(MT15_MAC),
            '{"ts":"2026-10-25T17:00:00Z","buttonPressed":false}',
            False,
            id="not_pressed",
        ),
    ],
)
@pytest.mark.usefixtures("mqtt_mock", "button_sensors", "freezer")
async def test_ignored_messages(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    topic: str,
    payload: str,
    retain: bool,  # noqa: FBT001
) -> None:
    """Old presses, other sensors and unreadable messages fire nothing."""
    await _setup(hass, mock_config_entry)
    async_fire_mqtt_message(hass, topic, payload, retain=retain)
    await hass.async_block_till_done()
    assert hass.states.get(MT15_BUTTON).state == STATE_UNKNOWN


@pytest.mark.usefixtures("button_sensors", "freezer")
async def test_no_mqtt(hass: HomeAssistant, mock_config_entry: MockConfigEntry) -> None:
    """Without MQTT, presses would arrive too late, so there are no buttons."""
    await _setup(hass, mock_config_entry)
    assert hass.states.get(MT15_BUTTON) is None
    assert hass.states.get(MT30_BUTTON) is None

"""Tests for MV Sense data that Meraki cameras publish over MQTT."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
    async_fire_time_changed,
)

from custom_components.cisco_meraki.camera_sense import (
    AUDIO_LEVEL,
    LIGHT,
    SenseValue,
    parse_message,
    parse_recent,
)
from custom_components.cisco_meraki.const import DEFAULT_SCAN_INTERVAL

from .conftest import make_api_error

CAMERA = "Q2BB-BBBB-BBBB"
PREFIX = f"/merakimv/{CAMERA}"
ENTRANCE = "619244948763443982"
FULL_FRAME_PEOPLE = "sensor.mv12_full_frame_people"


@pytest.mark.parametrize(
    ("subtopic", "payload", "expected"),
    [
        pytest.param(
            "0",
            '{"ts":1792523619482, "counts":{"person":3}}',
            [SenseValue(CAMERA, "zone_0_person", 3, "0", "person")],
            id="zone_count",
        ),
        pytest.param(
            ENTRANCE,
            '{"ts":1792523619482, "counts":{"person":1, "vehicle":2}}',
            [
                SenseValue(CAMERA, f"zone_{ENTRANCE}_person", 1, ENTRANCE, "person"),
                SenseValue(CAMERA, f"zone_{ENTRANCE}_vehicle", 2, ENTRANCE, "vehicle"),
            ],
            id="vehicles",
        ),
        pytest.param(
            "audio_analytics",
            '{\n\t"audioLevel" : -75,\n\t"ts" : 1792523620311\n}\n',
            [SenseValue(CAMERA, AUDIO_LEVEL, -75)],
            id="audio_level",
        ),
        pytest.param(
            "light",
            '{"lux": 136.2, "ts": 1792523620311}',
            [SenseValue(CAMERA, LIGHT, 136.2)],
            id="light",
        ),
        pytest.param(
            "raw_detections",
            '{\n\t"objects" : [],\n\t"ts" : 1792523620481\n}',
            [],
            id="raw_detections",
        ),
        pytest.param("0", "not json", [], id="invalid"),
    ],
)
def test_parse_message(subtopic: str, payload: str, expected: list) -> None:
    """MV Sense payloads, as an MV2 publishes them, are understood."""
    assert parse_message(f"{PREFIX}/{subtopic}", payload) == expected


def test_parse_message_ignores_other_topics() -> None:
    """Topics outside /merakimv/<serial>/<subtopic> are ignored."""
    assert parse_message("merakimv/x/0", '{"counts":{"person":1}}') == []
    assert parse_message("/merakimv/x/0/extra", '{"counts":{"person":1}}') == []


# The tests freeze the clock: the mocked MQTT client re-arms a timer every second,
# which would otherwise be pending whenever a test ends.


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


@pytest.mark.usefixtures("mqtt_mock", "mock_client", "freezer")
async def test_camera_sense_sensors(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    """Cameras get a sensor per zone and object type once they publish."""
    await _setup(hass, mock_config_entry)
    assert hass.states.get(FULL_FRAME_PEOPLE) is None

    async_fire_mqtt_message(hass, f"{PREFIX}/0", '{"ts":1, "counts":{"person":0}}')
    await hass.async_block_till_done()
    people = hass.states.get(FULL_FRAME_PEOPLE)
    assert people.state == "0"
    assert people.attributes["state_class"] == "measurement"

    async_fire_mqtt_message(hass, f"{PREFIX}/0", '{"ts":2, "counts":{"person":2}}')
    await hass.async_block_till_done()
    assert hass.states.get(FULL_FRAME_PEOPLE).state == "2"

    # Zones are named as in the Dashboard; vehicle counts get their own sensor.
    async_fire_mqtt_message(
        hass, f"{PREFIX}/{ENTRANCE}", '{"ts":3, "counts":{"person":1, "vehicle":1}}'
    )
    async_fire_mqtt_message(hass, f"{PREFIX}/light", '{"lux": 120.5}')
    async_fire_mqtt_message(hass, f"{PREFIX}/audio_analytics", '{"audioLevel": -70}')
    await hass.async_block_till_done()
    assert hass.states.get("sensor.mv12_entrance_people").state == "1"
    assert hass.states.get("sensor.mv12_entrance_vehicles").state == "1"
    assert hass.states.get("sensor.mv12_illuminance").state == "120.5"
    # The audio level is relative to the microphone, so it starts disabled.
    audio = entity_registry.async_get("sensor.mv12_audio_level")
    assert audio.disabled_by is er.RegistryEntryDisabler.INTEGRATION


@pytest.mark.usefixtures("mqtt_mock", "mock_client")
async def test_repeated_values_are_not_written(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Cameras repeat their counts several times a second; only changes count."""
    await _setup(hass, mock_config_entry)
    async_fire_mqtt_message(hass, f"{PREFIX}/0", '{"ts":1, "counts":{"person":1}}')
    await hass.async_block_till_done()
    first = hass.states.get(FULL_FRAME_PEOPLE).last_reported

    for ts in range(2, 7):
        freezer.tick(1)
        async_fire_mqtt_message(
            hass, f"{PREFIX}/0", f'{{"ts":{ts}, "counts":{{"person":1}}}}'
        )
        await hass.async_block_till_done()
    assert hass.states.get(FULL_FRAME_PEOPLE).last_reported == first


@pytest.mark.usefixtures("mqtt_mock", "mock_client", "freezer")
async def test_unknown_cameras_are_ignored(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Only cameras of the organization get sensors."""
    await _setup(hass, mock_config_entry)
    async_fire_mqtt_message(
        hass, "/merakimv/Q2ZZ-ZZZZ-ZZZZ/0", '{"ts":1, "counts":{"person":1}}'
    )
    await hass.async_block_till_done()
    assert not [s for s in hass.states.async_all() if "full_frame" in s.entity_id]


# As getDeviceCameraAnalyticsRecent returns it: the last minute of each zone.
RECENT = [
    {
        "zoneId": 0,
        "startTs": "2026-10-20T20:05:45.082Z",
        "endTs": "2026-10-20T20:06:45.082Z",
        "entrances": 1,
        "averageCount": 1.5,
    },
    {"zoneId": int(ENTRANCE), "error": "No updates in past 30 minutes"},
]


def test_parse_recent() -> None:
    """Zones without recent detections are skipped."""
    assert parse_recent(CAMERA, RECENT) == [
        SenseValue(CAMERA, "zone_0_person", 2, "0", "person")
    ]
    assert parse_recent(CAMERA, {"error": "Could not communicate with camera"}) == []


@pytest.mark.usefixtures("mqtt_mock")
async def test_api_fallback(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Cameras that do not publish over MQTT are polled through the API."""
    get_recent = mock_client.camera.getDeviceCameraAnalyticsRecent
    get_recent.return_value = RECENT
    await _setup(hass, mock_config_entry)
    # The minute's average of 1.5 people, in whole people.
    assert hass.states.get(FULL_FRAME_PEOPLE).state == "2"

    # Once the camera publishes over MQTT, the API is no longer polled for it.
    async_fire_mqtt_message(hass, f"{PREFIX}/0", '{"ts":1, "counts":{"person":3}}')
    await hass.async_block_till_done()
    assert hass.states.get(FULL_FRAME_PEOPLE).state == "3"
    get_recent.reset_mock()
    freezer.tick(DEFAULT_SCAN_INTERVAL + timedelta(seconds=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    get_recent.assert_not_awaited()

    # After two quiet minutes on MQTT, the API takes over again.
    for _ in range(2):
        freezer.tick(DEFAULT_SCAN_INTERVAL + timedelta(seconds=1))
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=True)
    get_recent.assert_awaited()
    assert hass.states.get(FULL_FRAME_PEOPLE).state == "2"


@pytest.mark.usefixtures("mqtt_mock", "freezer")
async def test_api_fallback_error_is_not_fatal(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """A camera the API cannot reach does not stop the other data."""
    mock_client.camera.getDeviceCameraAnalyticsRecent.side_effect = make_api_error(400)
    await _setup(hass, mock_config_entry)
    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert hass.states.get(FULL_FRAME_PEOPLE) is None

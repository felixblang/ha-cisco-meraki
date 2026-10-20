"""Tests for receiving Meraki sensor readings over MQTT."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.cisco_meraki.const import SENSOR_SCAN_INTERVAL
from custom_components.cisco_meraki.mqtt_source import parse_message

from .conftest import NETWORK_ID

AIR = "Q3AA-AAAA-AAAA"
AIR_MAC = "6C:7F:0C:11:2B:A2"
PREFIX = f"meraki/v1/mt/{NETWORK_ID}/ble/{AIR_MAC}"
NOW = datetime(2026, 10, 20, 12, 20, tzinfo=UTC)
STAT = "sensor.office_air_co2"


def _payload(ts: datetime, **values: Any) -> str:
    return json.dumps({"ts": ts.isoformat().replace("+00:00", "Z"), **values})


@pytest.mark.parametrize(
    ("metric", "payload", "expected"),
    [
        (
            "temperature",
            '{"ts":"2026-10-19T22:07:51Z","fahrenheit":82.1,"celsius":27.8}',
            ("temperature", 27.8),
        ),
        (
            "PM2_5MassConcentration",
            '{"ts":"2026-10-19T22:08:10Z","PM2_5MassConcentration":5,"units":"ug/m^3"}',
            ("pm25", 5.0),
        ),
        (
            "mainsRealPower",
            '{"ts":"2026-10-19T22:10:33Z","mainsRealPower":20.0,"units":"W"}',
            ("realPower", 20.0),
        ),
    ],
)
def test_parse_message(metric: str, payload: str, expected: tuple[str, float]) -> None:
    """Meraki's MQTT payloads, as published by real sensors, are understood."""
    parsed = parse_message(f"{PREFIX}/{metric}", payload)
    assert parsed is not None
    mac, reading = parsed
    assert mac == AIR_MAC.lower()
    assert (reading.metric, reading.value) == expected
    assert reading.ts.tzinfo is not None


@pytest.mark.parametrize(
    ("topic", "payload"),
    [
        # Text scores and power state are not numeric metrics.
        (f"{PREFIX}/aqmScores", '{"ts":"2026-10-19T22:08:09Z","CO2":"Fair"}'),
        # Signal strength per gateway has a longer topic.
        (f"{PREFIX}/gateway/E4:55:A8:58:45:EA/rssi", '{"ts":"2026-10-13T11:19:16Z"}'),
        (f"{PREFIX}/CO2", "not json"),
        (f"{PREFIX}/CO2", '{"CO2":700}'),
    ],
)
def test_parse_message_ignores(topic: str, payload: str) -> None:
    """Unsupported or malformed messages are ignored."""
    assert parse_message(topic, payload) is None


@pytest.fixture
def mock_air_sensor(mock_client: MagicMock) -> MagicMock:
    """Add an MT15 that the API knows, with readings until 12:00."""
    mock_client.organizations.getOrganizationDevices.return_value = [
        {
            "serial": AIR,
            "name": "Office Air",
            "model": "MT15",
            "mac": AIR_MAC.lower(),
            "productType": "sensor",
            "networkId": NETWORK_ID,
        }
    ]
    api_reading = {
        "serial": AIR,
        "ts": "2026-10-20T12:00:00Z",
        "metric": "co2",
        "co2": {"concentration": 500},
    }
    mock_client.sensor.getOrganizationSensorReadingsHistory = AsyncMock(
        return_value=[api_reading]
    )
    mock_client.sensor.getOrganizationSensorReadingsLatest = AsyncMock(
        return_value=[{"serial": AIR, "readings": [api_reading]}]
    )
    return mock_client


@pytest.mark.usefixtures("mqtt_mock")
async def test_mqtt_readings(
    hass: HomeAssistant,
    mock_air_sensor: MagicMock,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Readings from MQTT update the sensor and its statistics at their own times."""
    freezer.move_to(NOW)
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get("sensor.office_air_co2").state == "500.0"

    # A retained message is only the topic's last value and is ignored.
    async_fire_mqtt_message(hass, f"{PREFIX}/CO2", _payload(NOW, CO2=999), retain=True)
    # A batch of a sensor's readings, the oldest 20 minutes old.
    for minute in range(20):
        ts = NOW - timedelta(minutes=20 - minute)
        async_fire_mqtt_message(hass, f"{PREFIX}/CO2", _payload(ts, CO2=600 + minute))
    # Readings of unknown sensors are ignored.
    async_fire_mqtt_message(
        hass, "meraki/v1/mt/N_9/ble/00:00:00:00:00:00/CO2", _payload(NOW, CO2=1)
    )
    freezer.tick(timedelta(seconds=3))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)

    co2 = hass.states.get("sensor.office_air_co2")
    assert co2.state == "619.0"
    assert co2.attributes["measured_at"] == (NOW - timedelta(minutes=1)).isoformat()

    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        NOW - timedelta(hours=1),
        None,
        {STAT},
        "5minute",
        None,
        {"mean"},
    )
    starts = [dt_util.utc_from_timestamp(row["start"]) for row in stats[STAT]]
    # 12:00 holds the API reading and the first MQTT ones; the newest period
    # (12:15) is not complete yet.
    assert starts == [
        NOW - timedelta(minutes=20),
        NOW - timedelta(minutes=15),
        NOW - timedelta(minutes=10),
    ]

    # While the sensor publishes over MQTT, the API is not polled for it.
    history = mock_air_sensor.sensor.getOrganizationSensorReadingsHistory
    history.reset_mock()
    freezer.tick(SENSOR_SCAN_INTERVAL + timedelta(seconds=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    history.assert_not_awaited()

    # Once it has been quiet for 30 minutes, the API takes over again.
    for _ in range(6):
        freezer.tick(SENSOR_SCAN_INTERVAL + timedelta(seconds=1))
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=True)
    history.assert_awaited()

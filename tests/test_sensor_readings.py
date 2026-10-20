"""Tests for Meraki MT sensor readings and their statistics."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.cisco_meraki.const import SENSOR_SCAN_INTERVAL

from .conftest import NETWORK_ID

NOW = datetime(2026, 10, 20, 12, 20, tzinfo=UTC)
HOUR_11 = datetime(2026, 10, 20, 11, 0, tzinfo=UTC)
LAST_READING = datetime(2026, 10, 20, 12, 10, tzinfo=UTC)

AIR = "Q3AA-AAAA-AAAA"
POWER = "Q3PP-PPPP-PPPP"
# Measurements are written to the sensor's own statistics.
TEMPERATURE_STAT = "sensor.office_air_temperature"
ENERGY_STAT = "cisco_meraki:q3pp_pppp_pppp_energy"


def _readings(until: datetime) -> list[dict[str, Any]]:
    """Return a reading per minute from 11:00: 20/21 °C and a constant 600 W."""
    readings = []
    ts = HOUR_11
    while ts <= until:
        iso = ts.isoformat().replace("+00:00", "Z")
        readings.append(
            {
                "serial": AIR,
                "ts": iso,
                "metric": "temperature",
                "temperature": {"celsius": 20 + ts.minute % 2, "fahrenheit": 0},
            }
        )
        readings.append(
            {
                "serial": POWER,
                "ts": iso,
                "metric": "realPower",
                "realPower": {"draw": 600},
            }
        )
        readings.append({"serial": POWER, "ts": iso, "metric": "door", "door": {}})
        ts += timedelta(minutes=1)
    return readings


@pytest.fixture
def mock_sensors(mock_client: MagicMock) -> list[dict[str, Any]]:
    """Add an MT15 and an MT40 whose readings history ends at 12:10."""
    mock_client.organizations.getOrganizationDevices.return_value = [
        {
            "serial": AIR,
            "name": "Office Air",
            "model": "MT15",
            "productType": "sensor",
            "networkId": NETWORK_ID,
        },
        {
            "serial": POWER,
            "name": "Desk Plug",
            "model": "MT40",
            "productType": "sensor",
            "networkId": NETWORK_ID,
        },
    ]
    readings = _readings(LAST_READING)

    def history(_org: str, **kwargs: Any) -> list[dict[str, Any]]:
        t0 = dt_util.parse_datetime(kwargs["t0"])
        t1 = dt_util.parse_datetime(kwargs["t1"])
        # Like the real API, newest first.
        return [
            r for r in reversed(readings) if t0 <= dt_util.parse_datetime(r["ts"]) <= t1
        ]

    mock_client.sensor.getOrganizationSensorReadingsHistory = AsyncMock(
        side_effect=history
    )
    mock_client.sensor.getOrganizationSensorReadingsLatest = AsyncMock()
    mock_client.sensor.getOrganizationSensorReadingsLatest.return_value = [
        {"serial": AIR, "readings": [readings[-3]]},
        {"serial": POWER, "readings": [readings[-2]]},
    ]
    return readings


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)


async def _statistics(
    hass: HomeAssistant, statistic_id: str, period: str
) -> list[dict[str, Any]]:
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        HOUR_11 - timedelta(hours=1),
        None,
        {statistic_id},
        period,
        None,
        {"mean", "min", "max", "sum"},
    )
    return stats.get(statistic_id, [])


@pytest.mark.usefixtures("mock_sensors")
async def test_sensor_entities(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Each metric gets a sensor with the latest reading and its real time."""
    freezer.move_to(NOW)
    await _setup(hass, mock_config_entry)

    temperature = hass.states.get("sensor.office_air_temperature")
    assert temperature.state == "20.0"
    assert temperature.attributes["unit_of_measurement"] == "°C"
    assert temperature.attributes["measured_at"] == LAST_READING.isoformat()
    assert temperature.attributes["state_class"] == "measurement"

    assert hass.states.get("sensor.desk_plug_power").state == "600.0"
    energy = hass.states.get("sensor.desk_plug_energy")
    # 600 W for the 70 minutes from 11:00 to 12:10.
    assert float(energy.state) == pytest.approx(0.7)
    # A running sum gets its own statistics instead of a state class.
    assert energy.attributes["statistic_id"] == ENERGY_STAT
    assert "state_class" not in energy.attributes


@pytest.mark.usefixtures("mock_sensors")
async def test_statistics_use_reading_times(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Statistics are written for the periods in which readings were measured."""
    freezer.move_to(NOW)
    await _setup(hass, mock_config_entry)

    short = await _statistics(hass, TEMPERATURE_STAT, "5minute")
    # Every complete 5-minute period from 11:00 up to the last reading at 12:10.
    assert len(short) == 14
    assert dt_util.utc_from_timestamp(short[0]["start"]) == HOUR_11
    assert short[0]["min"] == 20
    assert short[0]["max"] == 21

    hourly = await _statistics(hass, TEMPERATURE_STAT, "hour")
    assert [dt_util.utc_from_timestamp(row["start"]) for row in hourly] == [HOUR_11]
    assert hourly[0]["mean"] == pytest.approx(20.5)

    energy = await _statistics(hass, ENERGY_STAT, "hour")
    assert energy[0]["sum"] == pytest.approx(0.6)
    energy_short = await _statistics(hass, ENERGY_STAT, "5minute")
    assert energy_short[0]["sum"] == pytest.approx(0.05)
    assert energy_short[-1]["sum"] == pytest.approx(0.7)


async def test_restart_resumes_after_last_hour(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_sensors: list[dict[str, Any]],
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """After a restart, readings are fetched again so no hour is left incomplete."""
    freezer.move_to(NOW)
    await _setup(hass, mock_config_entry)
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)

    history = mock_client.sensor.getOrganizationSensorReadingsHistory
    history.reset_mock()
    # Readings continue at 600 W until 13:10.
    mock_sensors[:] = _readings(LAST_READING + timedelta(hours=1))
    freezer.move_to(NOW + timedelta(hours=1))
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)

    first_t0 = dt_util.parse_datetime(history.call_args_list[0].kwargs["t0"])
    # The sensors' own statistics may still hold what Home Assistant compiled from
    # their states for the newest hours, so those are fetched and rewritten again.
    assert first_t0 == HOUR_11 - timedelta(hours=1)

    energy = await _statistics(hass, ENERGY_STAT, "hour")
    # The sum carries on from the 11:00 hour.
    assert [row["sum"] for row in energy] == pytest.approx([0.6, 1.2])
    assert float(hass.states.get("sensor.desk_plug_energy").state) == pytest.approx(1.3)


@pytest.mark.usefixtures("mock_sensors")
async def test_statistics_follow_display_unit(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Statistics are written in the unit the sensor is displayed in."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    freezer.move_to(NOW)
    await _setup(hass, mock_config_entry)

    temperature = hass.states.get("sensor.office_air_temperature")
    assert temperature.attributes["unit_of_measurement"] == "°F"
    hourly = await _statistics(hass, TEMPERATURE_STAT, "hour")
    # 20.5 °C
    assert hourly[0]["mean"] == pytest.approx(68.9)


@pytest.mark.usefixtures("mock_sensors")
async def test_statistics_wait_for_home_assistant(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """
    Nothing is written for periods Home Assistant has not compiled yet.

    Home Assistant inserts its own rows for every period it compiles; a row written
    before that makes its insert fail, e.g. while it catches up after a restart.
    """
    freezer.move_to(NOW)
    compiled_until = "custom_components.cisco_meraki.stats._compiled_until"
    with patch(compiled_until, return_value=HOUR_11):
        await _setup(hass, mock_config_entry)
    assert await _statistics(hass, TEMPERATURE_STAT, "5minute") == []
    assert await _statistics(hass, ENERGY_STAT, "hour") == []

    # Home Assistant has caught up; the next poll writes everything.
    with patch(compiled_until, return_value=NOW - timedelta(minutes=5)):
        freezer.tick(SENSOR_SCAN_INTERVAL + timedelta(seconds=1))
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=True)
        await async_wait_recording_done(hass)
    assert len(await _statistics(hass, TEMPERATURE_STAT, "5minute")) == 14
    assert len(await _statistics(hass, ENERGY_STAT, "hour")) == 1

"""Tests for turning Meraki's port usage intervals into energy and power."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.cisco_meraki.coordinator import _energy_slots

from .conftest import USAGE_START, _usage

T0 = datetime(2026, 10, 20, 19, 40, tzinfo=UTC)


def _at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


def test_merged_interval_is_spread() -> None:
    """An interval followed by a missing one covers both slots."""
    # Like Meraki reported it: 19:55 is missing and 19:50 has twice the energy.
    slots = _energy_slots([(_at(5), 0.4), (_at(10), 0.7), (_at(20), 0.4)])
    assert slots == [
        (_at(5), 0.4),
        (_at(10), pytest.approx(0.35)),
        (_at(15), pytest.approx(0.35)),
        (_at(20), 0.4),
    ]
    # No energy is lost or added.
    assert sum(wh for _, wh in slots) == pytest.approx(1.5)


def test_long_gap_is_not_spread() -> None:
    """After a longer gap the switch was not reporting; nothing is filled in."""
    slots = _energy_slots([(_at(0), 0.4), (_at(60), 0.4)])
    assert slots == [(_at(0), 0.4), (_at(60), 0.4)]


@pytest.mark.usefixtures("mock_client")
async def test_merged_interval_power(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The power of a merged interval is not doubled."""
    intervals = [_usage(USAGE_START + timedelta(minutes=5 * i), 0.1) for i in range(4)]
    # The newest interval is missing; its energy is reported with the one before.
    intervals[2]["energy"]["usage"]["total"] = 0.2
    del intervals[3]
    intervals.append(_usage(USAGE_START + timedelta(minutes=20), 0.1))
    get_usage = (
        mock_client.switch.getOrganizationSwitchPortsUsageHistoryByDeviceByInterval
    )
    get_usage.return_value["items"][0]["ports"][1]["intervals"] = intervals
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    await async_wait_recording_done(hass)

    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        USAGE_START,
        None,
        {"sensor.core_switch_port_2_poe_power"},
        "5minute",
        None,
        {"mean"},
    )
    rows = stats["sensor.core_switch_port_2_poe_power"]
    # 09:10 is not doubled and the missing 09:15 is filled in.
    assert [
        (dt_util.utc_from_timestamp(row["start"]).strftime("%H:%M"), row["mean"])
        for row in rows
    ] == [("09:00", 1.2), ("09:05", 1.2), ("09:10", 1.2), ("09:15", 1.2)]
    # Spreading moves energy between slots, it does not change the total.
    energy = hass.states.get("sensor.core_switch_port_2_poe_energy")
    assert float(energy.state) == pytest.approx(0.0005)

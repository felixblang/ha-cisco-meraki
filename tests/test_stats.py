"""Tests for aggregating sensor readings into statistics."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.cisco_meraki.stats import (
    MerakiStatistic,
    StatisticKind,
    build_metadata,
)

T0 = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)
# Late enough that the 10:00 hour may be written.
NOW = datetime(2026, 10, 20, 11, 30, tzinfo=UTC)


def _stat(kind: StatisticKind) -> MerakiStatistic:
    return MerakiStatistic(build_metadata("cisco_meraki:x", "X", "W", kind), kind)


def _minutes(stat: MerakiStatistic, values: dict[int, float]) -> None:
    for minute, value in values.items():
        stat.add(T0 + timedelta(minutes=minute), value)


def test_mean_rows() -> None:
    """Readings are averaged into 5-minute and hourly rows at their own times."""
    stat = _stat(StatisticKind.MEAN)
    _minutes(stat, {0: 10, 2: 20, 5: 30, 61: 40})

    short, hourly = stat.collect(NOW)
    assert short[0] == {
        "start": T0,
        "mean": 15,
        "min": 10,
        "max": 20,
    }
    assert short[1]["start"] == T0 + timedelta(minutes=5)
    assert short[1]["mean"] == 30
    # Periods without readings get no row.
    assert len(short) == 2
    assert hourly == [{"start": T0, "mean": 20, "min": 10, "max": 30}]

    # Nothing is written twice.
    assert stat.collect(NOW) == ([], [])


def test_incomplete_periods_wait() -> None:
    """A period is only written once a later reading shows it is complete."""
    stat = _stat(StatisticKind.MEAN)
    _minutes(stat, {0: 10, 3: 20})
    assert stat.collect(NOW) == ([], [])

    _minutes(stat, {6: 30})
    short, hourly = stat.collect(NOW)
    assert [row["start"] for row in short] == [T0]
    assert hourly == []


def test_hourly_waits_for_roll_up() -> None:
    """The hourly row waits until Home Assistant has rolled up the hour itself."""
    stat = _stat(StatisticKind.MEAN)
    _minutes(stat, {0: 10, 61: 20})
    _, hourly = stat.collect(T0 + timedelta(minutes=70))
    assert hourly == []
    _, hourly = stat.collect(T0 + timedelta(minutes=76))
    assert [row["start"] for row in hourly] == [T0]


def test_duplicates_and_old_readings_are_ignored() -> None:
    """Readings that arrive through two paths are only counted once."""
    stat = _stat(StatisticKind.MEAN)
    assert stat.add(T0, 10)
    assert not stat.add(T0, 10)
    assert stat.add(T0 + timedelta(minutes=61), 20)
    stat.collect(NOW)
    # The 10:00 hour is written; a reading for it can no longer be used.
    assert not stat.add(T0 + timedelta(minutes=30), 99)


def test_energy_is_integrated_from_power() -> None:
    """Energy is the integral of power, as a running sum at the right times."""
    stat = _stat(StatisticKind.ENERGY)
    stat.hour_sum = 5.0
    stat.hour_done = stat.short_done = T0
    # 600 W for the first 30 minutes, then 1200 W.
    _minutes(stat, dict.fromkeys(range(30), 600))
    _minutes(stat, dict.fromkeys(range(30, 62), 1200))

    short, hourly = stat.collect(NOW)
    # 600 W for 5 minutes is 0.05 kWh.
    assert short[0]["start"] == T0
    assert short[0]["sum"] == pytest.approx(5.05)
    assert short[0]["state"] == short[0]["sum"]
    # 0.3 kWh in the first half hour plus 0.6 kWh in the second.
    assert hourly[0]["sum"] == pytest.approx(5.9)
    assert stat.hour_sum == pytest.approx(5.9)
    assert stat.energy_total == pytest.approx(5.9 + 0.02)


def test_energy_skips_gaps() -> None:
    """Power is not assumed while a sensor was not reporting."""
    stat = _stat(StatisticKind.ENERGY)
    _minutes(stat, {0: 600, 1: 600, 40: 600, 41: 600})
    # Two minutes of 600 W; the 39 minutes in between are a gap.
    assert stat.energy_total == pytest.approx(0.02)

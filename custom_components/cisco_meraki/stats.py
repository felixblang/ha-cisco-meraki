"""
Long-term statistics with the sensors' own timestamps.

Home Assistant records entity states at the time they are written. Meraki sensors
upload their readings in batches, so those states would bunch up minutes after the
fact. Statistics can be written for past periods, so every reading is aggregated
into 5-minute and hourly statistics at the time it was measured instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from functools import partial
from typing import TYPE_CHECKING

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.const import DOMAIN as RECORDER_DOMAIN
from homeassistant.components.recorder.db_schema import StatisticsShortTerm
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    STATISTIC_UNIT_TO_UNIT_CONVERTER,
    StatisticsRow,
    async_add_external_statistics,
    async_import_statistics,
    get_last_statistics,
)
from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT, UnitOfEnergy
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .const import DOMAIN, LOGGER

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant

SHORT_TERM = timedelta(minutes=5)
HOUR = timedelta(hours=1)

# Home Assistant rolls the 5-minute statistics of each hour up into an hourly row
# shortly after the hour. Writing our hourly row only after that keeps the two from
# racing; ours then replaces the roll-up, which may have missed late readings.
HOURLY_WRITE_DELAY = timedelta(minutes=15)

# Home Assistant also compiles 5-minute statistics from the states of entities with a
# state class, right after each period ends. Ours replace those, so they are written
# only after Home Assistant is done with the period.
SHORT_TERM_WRITE_DELAY = timedelta(minutes=1)

# Power is not integrated across gaps longer than this, e.g. while a sensor is offline.
MAX_POWER_GAP = timedelta(minutes=10)


class StatisticKind(StrEnum):
    """How readings are aggregated."""

    MEAN = "mean"
    # Readings are power (W); energy is their integral over time.
    ENERGY = "energy"
    # Readings are the energy (Wh) used in the interval starting at their time.
    ENERGY_DELTA = "energy_delta"

    @property
    def is_energy(self) -> bool:
        """Return True for statistics that are a running energy sum."""
        return self is not StatisticKind.MEAN


def statistic_id(serial: str, key: str) -> str:
    """Return the external statistic ID for a sensor metric."""
    return f"{DOMAIN}:{serial.lower().replace('-', '_')}_{key.lower()}"


def _floor(ts: datetime, period: timedelta) -> datetime:
    seconds = int(period.total_seconds())
    return dt_util.utc_from_timestamp(int(ts.timestamp()) // seconds * seconds)


@dataclass(slots=True)
class MerakiStatistic:
    """Pending readings and progress of one statistic."""

    metadata: StatisticMetaData
    kind: StatisticKind
    # Set for statistics that belong to an entity: they are written to the entity's
    # own statistics, which its history graph shows, instead of to metadata's ID.
    entity_unique_id: str | None = None
    # Start of the first hour that has not been written yet.
    hour_done: datetime | None = None
    # Energy sum (kWh) at hour_done.
    hour_sum: float = 0.0
    # Start of the first 5-minute period that has not been written yet.
    short_done: datetime | None = None
    # Timestamp of the newest reading accepted.
    watermark: datetime | None = None
    # Readings from hour_done on, plus the one before it for energy integration.
    readings: list[tuple[datetime, float]] = field(default_factory=list)

    def add(self, ts: datetime, value: float) -> bool:
        """Add a reading; return False if it is a duplicate or too old."""
        if self.watermark is not None and ts <= self.watermark:
            return False
        if self.hour_done is None:
            # A new statistic starts with the hour of its first reading.
            self.hour_done = self.short_done = _floor(ts, HOUR)
        elif ts < self.hour_done:
            return False
        self.readings.append((ts, value))
        self.watermark = ts
        return True

    @property
    def energy_total(self) -> float | None:
        """Return the energy sum (kWh) up to the newest reading."""
        if self.hour_done is None or self.watermark is None:
            return None
        end = self.watermark
        if self.kind is StatisticKind.ENERGY_DELTA:
            # The newest reading covers the interval after its time.
            end += SHORT_TERM
        return self.hour_sum + self._energy(self.hour_done, end)

    def collect(self, now: datetime) -> tuple[list[StatisticData], list[StatisticData]]:
        """Return the 5-minute and hourly rows that are complete."""
        short_rows: list[StatisticData] = []
        hourly_rows: list[StatisticData] = []
        if self.watermark is None or self.hour_done is None or self.short_done is None:
            return short_rows, hourly_rows

        # A period is complete once a reading after its end has arrived; batches
        # arrive in order, so nothing older can follow.
        short_ready = min(
            _floor(self.watermark, SHORT_TERM),
            _floor(now - SHORT_TERM_WRITE_DELAY, SHORT_TERM),
        )
        while self.short_done + SHORT_TERM <= short_ready:
            if row := self._row(self.short_done, self.short_done + SHORT_TERM):
                short_rows.append(row)
            self.short_done += SHORT_TERM

        hour_ready = min(
            _floor(self.watermark, HOUR), _floor(now - HOURLY_WRITE_DELAY, HOUR)
        )
        while self.hour_done + HOUR <= min(hour_ready, self.short_done):
            end = self.hour_done + HOUR
            if row := self._row(self.hour_done, end):
                hourly_rows.append(row)
            if self.kind.is_energy:
                self.hour_sum += self._energy(self.hour_done, end)
            self.hour_done = end
            self._prune()

        return short_rows, hourly_rows

    def _row(self, start: datetime, end: datetime) -> StatisticData | None:
        if self.kind.is_energy:
            total = self.hour_sum + self._energy(self.hour_done or start, end)
            return StatisticData(start=start, state=total, sum=total)
        values = [value for ts, value in self.readings if start <= ts < end]
        if not values:
            return None
        return StatisticData(
            start=start,
            mean=sum(values) / len(values),
            min=min(values),
            max=max(values),
        )

    def _energy(self, start: datetime, end: datetime) -> float:
        """Return the energy (kWh) used between start and end."""
        if self.kind is StatisticKind.ENERGY_DELTA:
            return sum(wh for ts, wh in self.readings if start <= ts < end) / 1000
        # Integrate power (W) over time.
        watt_seconds = 0.0
        for (ts, watts), (next_ts, _) in zip(
            self.readings, self.readings[1:], strict=False
        ):
            if next_ts - ts > MAX_POWER_GAP:
                continue
            overlap = (min(next_ts, end) - max(ts, start)).total_seconds()
            if overlap > 0:
                watt_seconds += watts * overlap
        return watt_seconds / 3_600_000

    def _prune(self) -> None:
        hour_done = self.hour_done
        if hour_done is None:
            return
        keep = next(
            (i for i, (ts, _) in enumerate(self.readings) if ts >= hour_done),
            len(self.readings),
        )
        # Keep the last reading before the hour so energy can be integrated from it.
        self.readings = self.readings[max(keep - 1, 0) :]


def build_metadata(
    sid: str, name: str, unit: str | None, kind: StatisticKind
) -> StatisticMetaData:
    """Return the metadata for a statistic."""
    if kind.is_energy:
        unit = UnitOfEnergy.KILO_WATT_HOUR
    converter = STATISTIC_UNIT_TO_UNIT_CONVERTER.get(unit)
    return StatisticMetaData(
        mean_type=(
            StatisticMeanType.ARITHMETIC
            if kind is StatisticKind.MEAN
            else StatisticMeanType.NONE
        ),
        has_sum=kind.is_energy,
        name=name,
        source=DOMAIN,
        statistic_id=sid,
        unit_class=converter.UNIT_CLASS if converter else None,
        unit_of_measurement=unit,
    )


class StatisticsWriter:
    """Write statistics for many sensor metrics to the recorder."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the writer."""
        self._hass = hass
        self.statistics: dict[str, MerakiStatistic] = {}
        self._short_term_supported = True

    async def async_ensure(
        self,
        key: str,
        name: str,
        unit: str | None,
        kind: StatisticKind,
        entity_unique_id: str | None = None,
    ) -> MerakiStatistic:
        """Return a statistic, resuming after what was already written."""
        if stat := self.statistics.get(key):
            return stat
        stat = MerakiStatistic(
            build_metadata(key, name, unit, kind), kind, entity_unique_id
        )
        sid = self._entity_id(stat) if entity_unique_id else key
        if sid and (row := await self._async_last_hour(sid)):
            last_start = dt_util.utc_from_timestamp(row["start"])
            if entity_unique_id:
                # The newest hours may still hold what Home Assistant compiled from
                # the entity's states, so rewrite them from the readings.
                stat.hour_done = stat.short_done = last_start - HOUR
            else:
                # Readings since then are fetched again, so the 5-minute statistics
                # of the unfinished hour are rewritten consistently.
                stat.hour_done = stat.short_done = last_start + HOUR
                stat.hour_sum = row.get("sum") or 0.0
        self.statistics[key] = stat
        return stat

    async def _async_last_hour(self, sid: str) -> StatisticsRow | None:
        last = await get_instance(self._hass).async_add_executor_job(
            partial(
                get_last_statistics,
                self._hass,
                1,
                sid,
                convert_units=False,
                types={"sum"},
            )
        )
        rows = last.get(sid)
        return rows[0] if rows else None

    def _entity_id(self, stat: MerakiStatistic) -> str | None:
        if stat.entity_unique_id is None:
            return None
        return er.async_get(self._hass).async_get_entity_id(
            SENSOR_DOMAIN, DOMAIN, stat.entity_unique_id
        )

    def async_write(self, now: datetime) -> None:
        """Write all complete periods."""
        for stat in self.statistics.values():
            if stat.entity_unique_id is None:
                short_rows, hourly_rows = stat.collect(now)
                self._async_write_short_term(stat.metadata, short_rows)
                if hourly_rows:
                    async_add_external_statistics(
                        self._hass, stat.metadata, hourly_rows
                    )
                continue

            target = self._entity_target(stat)
            if target is None:
                # The entity is not added yet; keep the readings until it is.
                continue
            short_rows, hourly_rows = stat.collect(now)
            if target is _DISCARD:
                continue
            metadata, convert = target
            short_rows = [_convert(row, convert) for row in short_rows]
            hourly_rows = [_convert(row, convert) for row in hourly_rows]
            self._async_write_short_term(metadata, short_rows)
            if hourly_rows:
                async_import_statistics(self._hass, metadata, hourly_rows)

    def _entity_target(
        self, stat: MerakiStatistic
    ) -> tuple[StatisticMetaData, Callable[[float], float] | None] | object | None:
        """
        Return the metadata and unit conversion to write an entity's statistics.

        Returns None while the entity is not added yet, and _DISCARD if its
        statistics are not wanted.
        """
        entity_id = self._entity_id(stat)
        if entity_id is None:
            return None
        entry = er.async_get(self._hass).async_get(entity_id)
        if entry is not None and entry.disabled:
            return _DISCARD
        if (state := self._hass.states.get(entity_id)) is None:
            return None
        native_unit = stat.metadata["unit_of_measurement"]
        unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT, native_unit)
        convert = None
        if unit != native_unit:
            # The user picked another display unit; statistics are kept in it.
            converter = STATISTIC_UNIT_TO_UNIT_CONVERTER.get(native_unit)
            if converter is None or unit not in converter.VALID_UNITS:
                return _DISCARD
            convert = converter.converter_factory(native_unit, unit)
        converter = STATISTIC_UNIT_TO_UNIT_CONVERTER.get(unit)
        metadata = StatisticMetaData(
            mean_type=StatisticMeanType.ARITHMETIC,
            has_sum=False,
            name=None,
            source=RECORDER_DOMAIN,
            statistic_id=entity_id,
            unit_class=converter.UNIT_CLASS if converter else None,
            unit_of_measurement=unit,
        )
        return metadata, convert

    def _async_write_short_term(
        self, metadata: StatisticMetaData, rows: list[StatisticData]
    ) -> None:
        if not rows or not self._short_term_supported:
            return
        # Home Assistant only exposes an import for hourly statistics. The recorder
        # task it uses also accepts the 5-minute table, which is what makes 5-minute
        # graphs with the sensors' own timestamps possible. It is not a public API,
        # so stop using it rather than fail if it changes.
        try:
            get_instance(self._hass).async_import_statistics(
                metadata, rows, StatisticsShortTerm
            )
        except (AttributeError, TypeError) as err:
            LOGGER.warning(
                "Writing 5-minute statistics is not supported by this version of "
                "Home Assistant, only hourly statistics will be written: %s",
                err,
            )
            self._short_term_supported = False


# Marks entity statistics that are dropped, e.g. because the entity is disabled.
_DISCARD = object()


def _convert(
    row: StatisticData, convert: Callable[[float], float] | None
) -> StatisticData:
    if convert is None:
        return row
    converted = StatisticData(start=row["start"])
    for key in ("mean", "min", "max"):
        if key in row:
            converted[key] = convert(row[key])
    return converted

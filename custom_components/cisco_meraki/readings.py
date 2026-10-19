"""Meraki MT sensor metrics and how to read them."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import (
    PERCENTAGE,
    UnitOfApparentPower,
    UnitOfDensity,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfRatio,
    UnitOfSoundPressure,
    UnitOfTemperature,
)
from homeassistant.util import dt as dt_util

if TYPE_CHECKING:
    from datetime import datetime

# Energy is not reported by the sensors; it is integrated from this metric.
ENERGY_SOURCE_METRIC = "realPower"


@dataclass(frozen=True, slots=True)
class MerakiMetric:
    """A numeric metric reported by Meraki MT sensors."""

    key: str
    # Path to the value inside the reading, e.g. ("temperature", "celsius").
    path: tuple[str, ...]
    name: str
    unit: str | None
    device_class: SensorDeviceClass | None = None


METRICS: dict[str, MerakiMetric] = {
    metric.key: metric
    for metric in (
        MerakiMetric(
            "temperature",
            ("temperature", "celsius"),
            "Temperature",
            UnitOfTemperature.CELSIUS,
            SensorDeviceClass.TEMPERATURE,
        ),
        MerakiMetric(
            "humidity",
            ("humidity", "relativePercentage"),
            "Humidity",
            PERCENTAGE,
            SensorDeviceClass.HUMIDITY,
        ),
        MerakiMetric(
            "co2",
            ("co2", "concentration"),
            "CO2",
            UnitOfRatio.PARTS_PER_MILLION,
            SensorDeviceClass.CO2,
        ),
        MerakiMetric(
            "pm25",
            ("pm25", "concentration"),
            "PM2.5",
            UnitOfDensity.MICROGRAMS_PER_CUBIC_METER,
            SensorDeviceClass.PM25,
        ),
        MerakiMetric(
            "tvoc",
            ("tvoc", "concentration"),
            "TVOC",
            UnitOfDensity.MICROGRAMS_PER_CUBIC_METER,
            SensorDeviceClass.VOLATILE_ORGANIC_COMPOUNDS,
        ),
        MerakiMetric(
            "noise",
            ("noise", "ambient", "level"),
            "Noise",
            UnitOfSoundPressure.WEIGHTED_DECIBEL_A,
            SensorDeviceClass.SOUND_PRESSURE,
        ),
        MerakiMetric(
            "indoorAirQuality",
            ("indoorAirQuality", "score"),
            "Indoor air quality",
            None,
        ),
        MerakiMetric(
            "battery",
            ("battery", "percentage"),
            "Battery",
            PERCENTAGE,
            SensorDeviceClass.BATTERY,
        ),
        MerakiMetric(
            "realPower",
            ("realPower", "draw"),
            "Power",
            UnitOfPower.WATT,
            SensorDeviceClass.POWER,
        ),
        MerakiMetric(
            "apparentPower",
            ("apparentPower", "draw"),
            "Apparent power",
            UnitOfApparentPower.VOLT_AMPERE,
            SensorDeviceClass.APPARENT_POWER,
        ),
        MerakiMetric(
            "voltage",
            ("voltage", "level"),
            "Voltage",
            UnitOfElectricPotential.VOLT,
            SensorDeviceClass.VOLTAGE,
        ),
        MerakiMetric(
            "current",
            ("current", "draw"),
            "Current",
            UnitOfElectricCurrent.AMPERE,
            SensorDeviceClass.CURRENT,
        ),
        MerakiMetric(
            "frequency",
            ("frequency", "level"),
            "Frequency",
            UnitOfFrequency.HERTZ,
            SensorDeviceClass.FREQUENCY,
        ),
        MerakiMetric(
            "powerFactor",
            ("powerFactor", "percentage"),
            "Power factor",
            PERCENTAGE,
            SensorDeviceClass.POWER_FACTOR,
        ),
    )
}


@dataclass(frozen=True, slots=True)
class MerakiReading:
    """One timestamped measurement of one metric."""

    serial: str
    metric: str
    ts: datetime
    value: float


def parse_reading(serial: str, raw: dict[str, Any]) -> MerakiReading | None:
    """Return the reading for an API reading, or None if it is not supported."""
    metric = METRICS.get(raw.get("metric", ""))
    if metric is None or not (ts := dt_util.parse_datetime(raw.get("ts") or "")):
        return None
    value: Any = raw
    for key in metric.path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    if not isinstance(value, int | float) or isinstance(value, bool):
        return None
    return MerakiReading(serial, metric.key, dt_util.as_utc(ts), float(value))

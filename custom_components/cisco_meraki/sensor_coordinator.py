"""Coordinator for Meraki MT sensor readings."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.core import CALLBACK_TYPE, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from meraki import APIError

from .api import is_auth_error
from .const import DOMAIN, LOGGER, SENSOR_SCAN_INTERVAL
from .readings import ENERGY_SOURCE_METRIC, METRICS, MerakiReading, parse_reading
from .stats import (
    HOUR,
    MerakiStatistic,
    StatisticKind,
    StatisticsWriter,
    statistic_id,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from homeassistant.core import HomeAssistant
    from meraki.aio import AsyncDashboardAPI

    from .coordinator import MerakiConfigEntry, MerakiDataUpdateCoordinator

ENERGY_KEY = "energy"


def reading_unique_id(serial: str, key: str) -> str:
    """Return the unique ID of the sensor for a metric of an MT sensor."""
    return f"{serial}_{key}"


# How far back readings are fetched after a restart or an outage.
MAX_BACKFILL = timedelta(hours=24)
# Sensors upload in batches, so each poll re-reads at least this far back to pick
# up batches that arrived since, even for sensors that have been quiet for a while.
MAX_UPLOAD_LAG = timedelta(hours=2)
# The readings history is fetched in windows of this size to bound memory use;
# fast metrics such as PM2.5 report every second.
FETCH_WINDOW = timedelta(hours=1)

# Seconds to wait after an MQTT message before adding the buffered readings.
MQTT_FLUSH_DELAY = 2
# Sensors upload every 20 minutes; one that has not published over MQTT for longer
# than this is polled through the API again.
MQTT_FRESH = timedelta(minutes=30)

type LatestReadings = dict[tuple[str, str], MerakiReading]


class MerakiSensorCoordinator(DataUpdateCoordinator[LatestReadings]):
    """Fetch MT sensor readings and write their statistics."""

    config_entry: MerakiConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: MerakiConfigEntry,
        client: AsyncDashboardAPI,
        devices: MerakiDataUpdateCoordinator,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            LOGGER,
            config_entry=config_entry,
            name=f"{DOMAIN}_sensors",
            update_interval=SENSOR_SCAN_INTERVAL,
        )
        self.client = client
        self.devices = devices
        self.writer = StatisticsWriter(hass)
        self._latest: LatestReadings = {}
        # MQTT readings arrive in bursts of thousands; they are buffered and added
        # together shortly after the burst.
        self._mqtt_buffer: list[MerakiReading] = []
        self._mqtt_flush_unsub: CALLBACK_TYPE | None = None
        # Serial -> when the sensor last delivered a reading over MQTT.
        self._mqtt_seen: dict[str, datetime] = {}
        config_entry.async_on_unload(self._async_cancel_mqtt_flush)

    @property
    def serials(self) -> list[str]:
        """Return the serials of the MT sensors."""
        return sorted(
            serial
            for serial, device in self.devices.data.devices.items()
            if device.product_type == "sensor"
        )

    @callback
    def async_add_mqtt_reading(self, mac: str, reading: MerakiReading) -> None:
        """Add a reading received over MQTT for the sensor with this MAC address."""
        serial = next(
            (
                serial
                for serial, device in self.devices.data.devices.items()
                if device.product_type == "sensor"
                and device.mac
                and device.mac.lower() == mac
            ),
            None,
        )
        if serial is None:
            return
        self._mqtt_buffer.append(replace(reading, serial=serial))
        self._mqtt_seen[serial] = dt_util.utcnow()
        if self._mqtt_flush_unsub is None:
            self._mqtt_flush_unsub = async_call_later(
                self.hass, MQTT_FLUSH_DELAY, self._async_flush_mqtt
            )

    async def _async_flush_mqtt(self, _now: datetime) -> None:
        self._mqtt_flush_unsub = None
        readings, self._mqtt_buffer = self._mqtt_buffer, []
        await self._async_add(sorted(readings, key=lambda reading: reading.ts))
        await self.writer.async_refresh_and_write()
        self.async_set_updated_data(dict(self._latest))

    @callback
    def _async_cancel_mqtt_flush(self) -> None:
        if self._mqtt_flush_unsub is not None:
            self._mqtt_flush_unsub()
            self._mqtt_flush_unsub = None

    def _publishes_mqtt(self, serial: str, now: datetime) -> bool:
        """Return True if the sensor is currently delivering its readings over MQTT."""
        seen = self._mqtt_seen.get(serial)
        return seen is not None and now - seen < MQTT_FRESH

    def energy_total(self, serial: str) -> float | None:
        """Return the energy (kWh) a power sensor has measured so far."""
        stat = self.writer.statistics.get(statistic_id(serial, ENERGY_KEY))
        return stat.energy_total if stat else None

    async def _async_update_data(self) -> LatestReadings:
        """Fetch new readings and write their statistics."""
        try:
            await self._async_fetch()
        except APIError as err:
            if is_auth_error(err):
                raise ConfigEntryAuthFailed(
                    translation_domain=DOMAIN, translation_key="invalid_auth"
                ) from err
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="update_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        finally:
            # Whatever was received is kept, so write it even if a later window failed.
            await self.writer.async_refresh_and_write()
        return dict(self._latest)

    async def _async_fetch(self) -> None:
        now = dt_util.utcnow()
        # Sensors publishing over MQTT need no polling.
        serials = [s for s in self.serials if not self._publishes_mqtt(s, now)]
        if not serials:
            return
        start = await self._async_fetch_start(serials, now)
        while start < now:
            end = min(start + FETCH_WINDOW, now)
            raw = await self.client.sensor.getOrganizationSensorReadingsHistory(
                self.devices.organization_id,
                total_pages="all",
                perPage=1000,
                t0=start.isoformat(),
                t1=end.isoformat(),
                serials=serials,
            )
            # The API returns the newest readings first; statistics need them in
            # the order they were measured.
            await self._async_add(
                sorted(
                    (
                        reading
                        for item in raw
                        if (reading := parse_reading(item.get("serial", ""), item))
                    ),
                    key=lambda reading: reading.ts,
                )
            )
            start = end

    async def _async_fetch_start(self, serials: list[str], now: datetime) -> datetime:
        """Return the time from which readings have to be fetched."""
        if not self.writer.statistics:
            # First run: find out which metrics exist and where their statistics
            # left off, so the gap since then can be filled.
            raw = await self.client.sensor.getOrganizationSensorReadingsLatest(
                self.devices.organization_id, total_pages="all", serials=serials
            )
            for sensor in raw:
                for item in sensor.get("readings", []):
                    if reading := parse_reading(sensor.get("serial", ""), item):
                        await self._async_statistics_for(reading)
            resume = [
                stat.hour_done
                for stat in self.writer.statistics.values()
                if stat.hour_done is not None
            ]
            # Metrics without statistics yet start with the previous full hour.
            start = min(
                resume, default=(now - HOUR).replace(minute=0, second=0, microsecond=0)
            )
            return max(start, now - MAX_BACKFILL)

        prefixes = tuple(statistic_id(serial, "") for serial in serials)
        watermarks = [
            stat.watermark
            for sid, stat in self.writer.statistics.items()
            if stat.watermark is not None and sid.startswith(prefixes)
        ]
        start = min(watermarks, default=now - MAX_UPLOAD_LAG)
        return max(start, now - MAX_UPLOAD_LAG)

    async def _async_add(self, readings: Iterable[MerakiReading]) -> None:
        for count, reading in enumerate(readings, start=1):
            if count % 1000 == 0:
                # A backfill holds tens of thousands of readings; let other work run.
                await asyncio.sleep(0)
            for stat in await self._async_statistics_for(reading):
                stat.add(reading.ts, reading.value)
            key = (reading.serial, reading.metric)
            latest = self._latest.get(key)
            if latest is None or reading.ts > latest.ts:
                self._latest[key] = reading

    async def _async_statistics_for(
        self, reading: MerakiReading
    ) -> list[MerakiStatistic]:
        """Return the statistics a reading feeds, creating them as needed."""
        device = self.devices.data.devices.get(reading.serial)
        device_name = device.name if device else reading.serial
        metric = METRICS[reading.metric]
        stats = [
            await self.writer.async_ensure(
                statistic_id(reading.serial, reading.metric),
                f"{device_name} {metric.name}",
                metric.unit,
                StatisticKind.MEAN,
                # Written to the sensor's own statistics, see MerakiReadingSensor.
                entity_unique_id=reading_unique_id(reading.serial, reading.metric),
            )
        ]
        if reading.metric == ENERGY_SOURCE_METRIC:
            stats.append(
                await self.writer.async_ensure(
                    statistic_id(reading.serial, ENERGY_KEY),
                    f"{device_name} Energy",
                    None,
                    StatisticKind.ENERGY,
                )
            )
        return stats

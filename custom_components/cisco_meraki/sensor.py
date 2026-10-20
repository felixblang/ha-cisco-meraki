"""Sensor platform for the Cisco Meraki integration."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfEnergy, UnitOfPower, UnitOfTime
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import CLIENT_PRODUCT_TYPES, DEVICE_STATUSES, DOMAIN, UPLINK_STATUSES
from .entity import MerakiEntity, MerakiPortEntity, async_setup_port_entities
from .readings import ENERGY_SOURCE_METRIC, METRICS
from .sensor_coordinator import (
    ENERGY_KEY,
    MerakiSensorCoordinator,
    reading_unique_id,
)
from .stats import statistic_id

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import (
        MerakiConfigEntry,
        MerakiDataUpdateCoordinator,
        MerakiDevice,
        MerakiUplink,
        MerakiUplinkQuality,
    )
    from .readings import MerakiMetric, MerakiReading

# The coordinator polls; entities never request their own updates.
PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class MerakiSensorEntityDescription(SensorEntityDescription):
    """Describes a Meraki sensor."""

    value_fn: Callable[[MerakiDevice], str | int | None]
    supported_fn: Callable[[MerakiDevice], bool] = lambda _: True


SENSORS: tuple[MerakiSensorEntityDescription, ...] = (
    MerakiSensorEntityDescription(
        key="status",
        translation_key="status",
        device_class=SensorDeviceClass.ENUM,
        options=DEVICE_STATUSES,
        value_fn=lambda device: (
            device.status if device.status in DEVICE_STATUSES else None
        ),
    ),
    MerakiSensorEntityDescription(
        key="clients",
        translation_key="clients",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda device: device.client_count,
        supported_fn=lambda device: device.product_type in CLIENT_PRODUCT_TYPES,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001
    entry: MerakiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Meraki sensors, adding new devices as they appear."""
    coordinator = entry.runtime_data
    known_serials: set[str] = set()

    @callback
    def _async_add_new_devices() -> None:
        new_serials = coordinator.data.devices.keys() - known_serials
        if not new_serials:
            return
        known_serials.update(new_serials)
        async_add_entities(
            MerakiSensor(coordinator, serial, description)
            for serial in new_serials
            for description in SENSORS
            if description.supported_fn(coordinator.data.devices[serial])
        )

    _async_add_new_devices()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_new_devices))

    if coordinator.sensors:
        _async_setup_reading_sensors(entry, coordinator.sensors, async_add_entities)

    def _has_poe_energy(
        coordinator: MerakiDataUpdateCoordinator, serial: str, port_id: str
    ) -> bool:
        return coordinator.port_energy_total(serial, port_id) is not None

    for factory in (MerakiPortPoePowerSensor, MerakiPortPoeEnergySensor):
        async_setup_port_entities(entry, async_add_entities, factory, _has_poe_energy)

    known_uplinks: set[tuple[str, str]] = set()
    known_quality: set[tuple[str, str]] = set()

    @callback
    def _async_add_new_uplinks() -> None:
        current = {
            (serial, interface)
            for serial, uplinks in coordinator.data.uplinks.items()
            for interface in uplinks
        }
        if new := current - known_uplinks:
            known_uplinks.update(new)
            async_add_entities(
                MerakiUplinkSensor(coordinator, serial, interface)
                for serial, interface in sorted(new)
            )
        if new_quality := coordinator.data.uplink_quality.keys() - known_quality:
            known_quality.update(new_quality)
            async_add_entities(
                sensor(coordinator, serial, interface)
                for serial, interface in sorted(new_quality)
                for sensor in (MerakiUplinkLatencySensor, MerakiUplinkLossSensor)
            )

    _async_add_new_uplinks()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_new_uplinks))


@callback
def _async_setup_reading_sensors(
    entry: MerakiConfigEntry,
    coordinator: MerakiSensorCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a sensor per MT sensor metric as soon as its first reading arrives."""
    known: set[tuple[str, str]] = set()

    @callback
    def _async_add_new_metrics() -> None:
        new = (coordinator.data or {}).keys() - known
        if not new:
            return
        known.update(new)
        entities: list[SensorEntity] = []
        for serial, metric in sorted(new):
            entities.append(MerakiReadingSensor(coordinator, serial, METRICS[metric]))
            if metric == ENERGY_SOURCE_METRIC:
                entities.append(MerakiEnergySensor(coordinator, serial))
        async_add_entities(entities)

    _async_add_new_metrics()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_new_metrics))


class MerakiSensor(MerakiEntity, SensorEntity):
    """A sensor reporting one value of a Meraki device."""

    entity_description: MerakiSensorEntityDescription

    def __init__(
        self,
        coordinator: MerakiDataUpdateCoordinator,
        serial: str,
        description: MerakiSensorEntityDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, serial, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> str | int | None:
        """Return the sensor value."""
        return self.entity_description.value_fn(self.device)


class MerakiSensorReadingEntity(
    CoordinatorEntity[MerakiSensorCoordinator], SensorEntity
):
    """A value measured by a Meraki MT sensor."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: MerakiSensorCoordinator, serial: str, key: str
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._serial = serial
        self._attr_unique_id = reading_unique_id(serial, key)
        self._attr_translation_key = _snake(key)
        # The device is registered with its details by the device status sensor.
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, serial)})


class MerakiReadingSensor(MerakiSensorReadingEntity):
    """
    The latest reading of one metric of a Meraki MT sensor.

    The state is written when a batch of readings arrives, minutes after they were
    measured. Home Assistant compiles the sensor's statistics from its states right
    after each 5-minute period; the integration then replaces them with statistics
    computed from the readings at the times they were measured.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self, coordinator: MerakiSensorCoordinator, serial: str, metric: MerakiMetric
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, serial, metric.key)
        self._metric = metric.key
        self._attr_device_class = metric.device_class
        self._attr_native_unit_of_measurement = metric.unit

    async def async_added_to_hass(self) -> None:
        """Write the statistics that waited for this sensor to be added."""
        await super().async_added_to_hass()
        # Runs right after the sensor's first state is written.
        self.hass.loop.call_soon(self._async_write_statistics)

    @callback
    def _async_write_statistics(self) -> None:
        self.coordinator.writer.async_write(dt_util.utcnow())

    @property
    def _reading(self) -> MerakiReading | None:
        return (self.coordinator.data or {}).get((self._serial, self._metric))

    @property
    def available(self) -> bool:
        """Return True once the metric has a reading."""
        return super().available and self._reading is not None

    @property
    def native_value(self) -> float | None:
        """Return the latest reading."""
        reading = self._reading
        return reading.value if reading else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return when the reading was measured."""
        reading = self._reading
        return {"measured_at": reading.ts.isoformat() if reading else None}


class MerakiEnergySensor(MerakiSensorReadingEntity):
    """Energy used, integrated from a Meraki MT40's power readings."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 3

    def __init__(self, coordinator: MerakiSensorCoordinator, serial: str) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, serial, ENERGY_KEY)
        # No state class: a running sum cannot be corrected after Home Assistant has
        # compiled it, so correctly timed statistics are written to their own ID.
        self._statistic_id = statistic_id(serial, ENERGY_KEY)

    @property
    def native_value(self) -> float | None:
        """Return the energy used since the integration started measuring."""
        return self.coordinator.energy_total(self._serial)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return where the energy statistics are, for the energy dashboard."""
        return {"statistic_id": self._statistic_id}


def _snake(key: str) -> str:
    """Return a camelCase metric key as snake_case for translation keys."""
    return "".join(f"_{c.lower()}" if c.isupper() else c for c in key)


class MerakiPortPoePowerSensor(MerakiPortEntity, SensorEntity):
    """PoE power a switch port delivered during the newest reported interval."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_suggested_display_precision = 1
    _attr_translation_key = "port_poe_power"

    def __init__(
        self, coordinator: MerakiDataUpdateCoordinator, serial: str, port_id: str
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, serial, port_id, "poe_power")

    @property
    def native_value(self) -> float | None:
        """Return the average PoE power of the newest 5-minute interval."""
        return self.port.poe_power


class MerakiPortPoeEnergySensor(MerakiPortEntity, SensorEntity):
    """PoE energy a switch port has delivered."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 3
    _attr_translation_key = "port_poe_energy"

    def __init__(
        self, coordinator: MerakiDataUpdateCoordinator, serial: str, port_id: str
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, serial, port_id, "poe_energy")
        # No state class, like the MT sensors: correctly timed statistics are
        # written separately, see the statistic_id attribute.
        self._statistic_id = statistic_id(serial, f"port_{port_id}_poe_energy")

    @property
    def native_value(self) -> float | None:
        """Return the energy delivered since the integration started measuring."""
        return self.coordinator.port_energy_total(self._serial, self._port_id)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return where the energy statistics are, for the energy dashboard."""
        return {"statistic_id": self._statistic_id}


class MerakiUplinkSensor(MerakiEntity, SensorEntity):
    """Status of a WAN uplink of an appliance or gateway."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = UPLINK_STATUSES

    def __init__(
        self, coordinator: MerakiDataUpdateCoordinator, serial: str, interface: str
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, serial, f"uplink_{interface}")
        self._interface = interface
        self._attr_translation_key, self._attr_translation_placeholders = (
            _uplink_translation(interface)
        )

    @property
    def _uplink(self) -> MerakiUplink | None:
        return self.coordinator.data.uplinks.get(self._serial, {}).get(self._interface)

    @property
    def available(self) -> bool:
        """Return False if the uplink is no longer reported."""
        return super().available and self._uplink is not None

    @property
    def native_value(self) -> str | None:
        """Return the uplink status, e.g. active or failed."""
        uplink = self._uplink
        return uplink.status if uplink else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the uplink's addressing."""
        uplink = self._uplink
        if uplink is None:
            return {}
        return {
            "ip": uplink.ip,
            "gateway": uplink.gateway,
            "public_ip": uplink.public_ip,
            "primary_dns": uplink.primary_dns,
            "secondary_dns": uplink.secondary_dns,
            "ip_assigned_by": uplink.ip_assigned_by,
        }


class MerakiUplinkQualitySensor(MerakiEntity, SensorEntity):
    """
    Loss or latency of a WAN uplink, measured by the appliance every minute.

    Like the MT sensors, the measurements arrive minutes late; their statistics are
    rewritten with the measurement times, see MerakiReadingSensor.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1
    _metric: str

    def __init__(
        self, coordinator: MerakiDataUpdateCoordinator, serial: str, interface: str
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, serial, f"uplink_{interface}_{self._metric}")
        self._interface = interface
        key, placeholders = _uplink_translation(interface)
        self._attr_translation_key = f"{key}_{self._metric}"
        self._attr_translation_placeholders = placeholders

    async def async_added_to_hass(self) -> None:
        """Write the statistics that waited for this sensor to be added."""
        await super().async_added_to_hass()
        self.hass.loop.call_soon(self._async_write_statistics)

    @callback
    def _async_write_statistics(self) -> None:
        self.coordinator.uplink_stats.async_write(dt_util.utcnow())

    @property
    def _quality(self) -> MerakiUplinkQuality | None:
        return self.coordinator.data.uplink_quality.get((self._serial, self._interface))

    @property
    def available(self) -> bool:
        """Return False until a measurement is available."""
        return super().available and self._quality is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return when and against which destination it was measured."""
        quality = self._quality
        if quality is None:
            return {}
        return {
            "measured_at": quality.ts.isoformat(),
            "destination": quality.destination,
        }


class MerakiUplinkLatencySensor(MerakiUplinkQualitySensor):
    """Latency of a WAN uplink."""

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MILLISECONDS
    _metric = "latency"

    @property
    def native_value(self) -> float | None:
        """Return the newest latency."""
        quality = self._quality
        return quality.latency if quality else None


class MerakiUplinkLossSensor(MerakiUplinkQualitySensor):
    """Packet loss of a WAN uplink."""

    _attr_native_unit_of_measurement = PERCENTAGE
    _metric = "loss"

    @property
    def native_value(self) -> float | None:
        """Return the newest packet loss."""
        quality = self._quality
        return quality.loss if quality else None


def _uplink_translation(interface: str) -> tuple[str, dict[str, str]]:
    """Return the translation key and placeholders naming an uplink."""
    if match := re.fullmatch(r"wan(\d+)", interface):
        return "uplink_wan", {"number": match.group(1)}
    if interface == "cellular":
        return "uplink_cellular", {}
    return "uplink", {"interface": interface}

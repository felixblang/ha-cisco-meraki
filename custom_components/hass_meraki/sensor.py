"""Sensor platform for the Meraki Dashboard integration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.core import callback

from .const import CLIENT_PRODUCT_TYPES, DEVICE_STATUSES
from .entity import MerakiEntity

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import (
        MerakiConfigEntry,
        MerakiDataUpdateCoordinator,
        MerakiDevice,
    )

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

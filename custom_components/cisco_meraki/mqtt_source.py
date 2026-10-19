"""
Receive Meraki MT sensor readings from MQTT.

Meraki networks can publish sensor readings to an MQTT broker. When Home Assistant's
MQTT integration is connected to that broker, readings are taken from there instead
of the Dashboard API: the data stays local and the API is only polled for sensors
that are not publishing.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.util import dt as dt_util

from .const import LOGGER
from .readings import MerakiReading

if TYPE_CHECKING:
    from homeassistant.components.mqtt import ReceiveMessage

    from .sensor_coordinator import MerakiSensorCoordinator

# meraki/v1/mt/<network ID>/ble/<sensor MAC>/<metric>
MQTT_TOPIC = "meraki/v1/mt/+/ble/+/+"

# MQTT metric name -> (metric key, payload field). The names differ from the API's.
MQTT_METRICS: dict[str, tuple[str, str]] = {
    "temperature": ("temperature", "celsius"),
    "humidity": ("humidity", "humidity"),
    "CO2": ("co2", "CO2"),
    "PM2_5MassConcentration": ("pm25", "PM2_5MassConcentration"),
    "tvoc": ("tvoc", "tvoc"),
    "ambientNoise": ("noise", "ambientNoise"),
    "iaqIndex": ("indoorAirQuality", "iaqIndex"),
    "mainsRealPower": ("realPower", "mainsRealPower"),
    "mainsApparentPower": ("apparentPower", "mainsApparentPower"),
    "mainsVolts": ("voltage", "mainsVolts"),
    "mainsCurrent": ("current", "mainsCurrent"),
    "mainsFrequency": ("frequency", "mainsFrequency"),
    "mainsPowerFactor": ("powerFactor", "mainsPowerFactor"),
}


def parse_message(topic: str, payload: Any) -> tuple[str, MerakiReading] | None:
    """Return (sensor MAC, reading) for a Meraki MQTT message, if it is supported."""
    parts = topic.split("/")
    if len(parts) != len(MQTT_TOPIC.split("/")):
        return None
    if (metric := MQTT_METRICS.get(parts[-1])) is None:
        return None
    try:
        data = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    key, field = metric
    value = data.get(field)
    ts = dt_util.parse_datetime(data.get("ts") or "")
    if ts is None or not isinstance(value, int | float) or isinstance(value, bool):
        return None
    # The serial is filled in by the caller, which knows the devices.
    return parts[-2].lower(), MerakiReading("", key, dt_util.as_utc(ts), float(value))


async def async_subscribe(
    hass: HomeAssistant, coordinator: MerakiSensorCoordinator
) -> CALLBACK_TYPE | None:
    """Subscribe to Meraki sensor readings, if the MQTT integration is set up."""
    if "mqtt" not in hass.config.components:
        return None
    # Imported here: the MQTT integration and its client library are optional.
    from homeassistant.components import mqtt  # noqa: PLC0415

    if not await mqtt.async_wait_for_mqtt_client(hass):
        return None

    @callback
    def _async_message(msg: ReceiveMessage) -> None:
        # Retained messages repeat the last value of each topic; the API has already
        # delivered it, and replaying it could get ahead of the API's backfill.
        if msg.retain or (parsed := parse_message(msg.topic, msg.payload)) is None:
            return
        mac, reading = parsed
        coordinator.async_add_mqtt_reading(mac, reading)

    unsubscribe = await mqtt.async_subscribe(hass, MQTT_TOPIC, _async_message)
    LOGGER.debug("Receiving Meraki sensor readings from MQTT topic %s", MQTT_TOPIC)
    return unsubscribe

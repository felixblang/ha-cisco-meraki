"""
Meraki MV Sense: object counts, light and audio level of MV cameras.

Cameras publish these to MQTT in real time (within a few seconds), so their values are
plain sensor states and Home Assistant compiles their statistics itself. Cameras that
do not publish over MQTT are polled through the Dashboard API instead, which only
provides each zone's average people count of the last minute.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.util import dt as dt_util
from meraki import APIError

from .const import LOGGER

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.components.mqtt import ReceiveMessage
    from meraki.aio import AsyncDashboardAPI

# /merakimv/<camera serial>/<zone ID | light | audio_analytics | ...>
MQTT_TOPIC = "/merakimv/+/+"

# Object types a zone counts; MV2 and other indoor cameras only count people.
OBJECT_TYPES = ("person", "vehicle")

# Cameras publish counts several times a second; one that has been quiet on MQTT for
# this long is polled through the API.
MQTT_FRESH = timedelta(minutes=2)

LIGHT = "light"
AUDIO_LEVEL = "audio_level"


@dataclass(frozen=True, slots=True)
class SenseValue:
    """A value published by a camera: an object count, light or audio level."""

    serial: str
    key: str
    # Counts are whole numbers, light and audio level are not.
    value: int | float
    # Zone ID and object type, for zone counts.
    zone_id: str | None = None
    object_type: str | None = None


# Subtopics with a single value: subtopic -> (key, payload field).
SCALAR_SUBTOPICS = {
    "light": (LIGHT, "lux"),
    "audio_analytics": (AUDIO_LEVEL, "audioLevel"),
}


def parse_message(topic: str, payload: Any) -> list[SenseValue]:
    """Return the values in a Meraki MV MQTT message."""
    match topic.split("/"):
        case ["", "merakimv", serial, subtopic]:
            pass
        case _:
            return []
    try:
        data = json.loads(payload)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return []

    if subtopic in SCALAR_SUBTOPICS:
        key, field = SCALAR_SUBTOPICS[subtopic]
        value = data.get(field)
        return [SenseValue(serial, key, float(value))] if _is_number(value) else []

    # Zone topics carry counts; raw_detections, audio_detections and the like do not.
    counts = data.get("counts")
    if not isinstance(counts, dict):
        return []
    return [
        SenseValue(
            serial,
            f"zone_{subtopic}_{object_type}",
            int(count),
            zone_id=subtopic,
            object_type=object_type,
        )
        for object_type in OBJECT_TYPES
        if _is_number(count := counts.get(object_type))
    ]


def parse_recent(serial: str, zones: Any) -> list[SenseValue]:
    """
    Return the people counts in a getDeviceCameraAnalyticsRecent response.

    The API reports each zone's average count over the last minute; it is rounded
    to whole people, like the counts cameras publish over MQTT.
    """
    if not isinstance(zones, list):
        return []
    # Zones without detections in the last 30 minutes have an "error" instead.
    return [
        SenseValue(
            serial,
            f"zone_{zone['zoneId']}_person",
            math.floor(zone["averageCount"] + 0.5),
            zone_id=str(zone["zoneId"]),
            object_type="person",
        )
        for zone in zones
        if isinstance(zone, dict)
        and zone.get("zoneId") is not None
        and _is_number(zone.get("averageCount"))
    ]


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


class MerakiCameraSense:
    """The latest MV Sense values of an organization's cameras."""

    def __init__(self, serials: list[str]) -> None:
        """Initialize for the given camera serials."""
        self.serials = set(serials)
        # Camera serial -> zone ID -> zone name.
        self.zone_names: dict[str, dict[str, str]] = {}
        self.values: dict[tuple[str, str], SenseValue] = {}
        self._new_value_listeners: list[Callable[[SenseValue], None]] = []
        self._update_listeners: dict[tuple[str, str], list[Callable[[], None]]] = {}
        # Camera serial -> when it last published over MQTT.
        self._mqtt_seen: dict[str, datetime] = {}

    async def async_poll(self, client: AsyncDashboardAPI) -> None:
        """Poll the cameras that are not publishing over MQTT."""
        now = dt_util.utcnow()
        for serial in sorted(self.serials):
            seen = self._mqtt_seen.get(serial)
            if seen is not None and now - seen < MQTT_FRESH:
                continue
            try:
                zones = await client.camera.getDeviceCameraAnalyticsRecent(serial)
            except APIError as err:
                LOGGER.debug("Could not fetch analytics of %s: %s", serial, err)
                continue
            for value in parse_recent(serial, zones):
                self.async_set(value)

    @callback
    def async_mqtt_received(self, value: SenseValue) -> None:
        """Store a value received over MQTT."""
        if value.serial in self.serials:
            self._mqtt_seen[value.serial] = dt_util.utcnow()
        self.async_set(value)

    async def async_load_zone_names(self, client: AsyncDashboardAPI) -> None:
        """Look up the names of the cameras' analytics zones."""
        for serial in self.serials:
            try:
                zones = await client.camera.getDeviceCameraAnalyticsZones(serial)
            except APIError as err:
                LOGGER.debug("Could not fetch the zones of %s: %s", serial, err)
                continue
            # The full-frame zone has "zoneId" and "label", drawn zones "id" and "name".
            self.zone_names[serial] = {
                str(zone_id): zone.get("name") or zone.get("label") or str(zone_id)
                for zone in zones
                if (zone_id := zone.get("id") or zone.get("zoneId")) is not None
            }

    def zone_name(self, serial: str, zone_id: str) -> str:
        """Return a zone's name, or its ID if the name is unknown."""
        return self.zone_names.get(serial, {}).get(zone_id, zone_id)

    @callback
    def async_add_new_value_listener(
        self, listener: Callable[[SenseValue], None]
    ) -> CALLBACK_TYPE:
        """Call listener with the first value of each camera, zone and type."""
        self._new_value_listeners.append(listener)
        return lambda: self._new_value_listeners.remove(listener)

    @callback
    def async_add_update_listener(
        self, serial: str, key: str, listener: Callable[[], None]
    ) -> CALLBACK_TYPE:
        """Call listener when the value of a camera's key changes."""
        listeners = self._update_listeners.setdefault((serial, key), [])
        listeners.append(listener)
        return lambda: listeners.remove(listener)

    @callback
    def async_set(self, value: SenseValue) -> None:
        """Store a value and notify listeners if it changed."""
        if value.serial not in self.serials:
            return
        key = (value.serial, value.key)
        previous = self.values.get(key)
        # Cameras publish counts several times a second; most repeat the last one.
        if previous is not None and previous.value == value.value:
            return
        self.values[key] = value
        if previous is None:
            for new_value_listener in list(self._new_value_listeners):
                new_value_listener(value)
        for listener in list(self._update_listeners.get(key, [])):
            listener()


async def async_subscribe(
    hass: HomeAssistant, sense: MerakiCameraSense
) -> CALLBACK_TYPE | None:
    """Subscribe to the cameras' MV Sense topics, if the MQTT integration is set up."""
    if "mqtt" not in hass.config.components:
        return None
    # Imported here: the MQTT integration and its client library are optional.
    from homeassistant.components import mqtt  # noqa: PLC0415

    if not await mqtt.async_wait_for_mqtt_client(hass):
        return None

    @callback
    def _async_message(msg: ReceiveMessage) -> None:
        for value in parse_message(msg.topic, msg.payload):
            sense.async_mqtt_received(value)

    unsubscribe = await mqtt.async_subscribe(hass, MQTT_TOPIC, _async_message)
    LOGGER.debug("Receiving MV Sense data from MQTT topic %s", MQTT_TOPIC)
    return unsubscribe

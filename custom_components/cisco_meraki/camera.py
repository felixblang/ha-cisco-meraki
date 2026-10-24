"""Camera platform for the Cisco Meraki integration."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from homeassistant.components import ffmpeg
from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.components.stream import CONF_RTSP_TRANSPORT
from homeassistant.core import callback

from .const import LOGGER
from .entity import MerakiEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import MerakiConfigEntry, MerakiDataUpdateCoordinator

PARALLEL_UPDATES = 0

# ffmpeg waits 15 seconds for a camera it cannot reach, and dashboards ask for a new
# image every few seconds, so cameras are checked for being reachable first.
CONNECT_TIMEOUT = 3


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001
    entry: MerakiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up a camera per MV camera, streamed over its RTSP stream."""
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _async_add_new_cameras() -> None:
        if new := coordinator.data.camera_video.keys() - known:
            known.update(new)
            async_add_entities(
                MerakiCamera(coordinator, serial) for serial in sorted(new)
            )

    _async_add_new_cameras()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_new_cameras))


class MerakiCamera(MerakiEntity, Camera):
    """An MV camera, available while its RTSP stream is exposed on the LAN."""

    _attr_name = None
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, coordinator: MerakiDataUpdateCoordinator, serial: str) -> None:
        """Initialize the camera."""
        super().__init__(coordinator, serial, "camera")
        Camera.__init__(self)
        # Over UDP, the stream often does not get through.
        self.stream_options[CONF_RTSP_TRANSPORT] = "tcp"

    @property
    def _rtsp_url(self) -> str | None:
        video = self.coordinator.data.camera_video.get(self._serial)
        return video.rtsp_url if video else None

    @property
    def available(self) -> bool:
        """Return False while the camera does not expose its RTSP stream."""
        return super().available and self._rtsp_url is not None

    async def stream_source(self) -> str | None:
        """Return the camera's RTSP stream."""
        return self._rtsp_url

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return a frame of the RTSP stream."""
        if (url := self._rtsp_url) is None or not await _async_reachable(url):
            return None
        return await ffmpeg.async_get_image(
            self.hass, f"-rtsp_transport tcp -i {url}", width=width, height=height
        )


async def _async_reachable(url: str) -> bool:
    """Return True if the RTSP stream's port accepts connections."""
    parts = urlsplit(url)
    try:
        async with asyncio.timeout(CONNECT_TIMEOUT):
            _, writer = await asyncio.open_connection(parts.hostname, parts.port or 554)
    except OSError, TimeoutError:
        LOGGER.debug("Cannot reach the RTSP stream %s", url)
        return False
    writer.close()
    await writer.wait_closed()
    return True

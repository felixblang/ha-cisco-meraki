"""Tests for MV cameras and their RTSP stream."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.camera import async_get_image, async_get_stream_source
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.components.switch import SERVICE_TURN_OFF, SERVICE_TURN_ON
from homeassistant.const import (
    ATTR_ENTITY_ID,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.cisco_meraki.camera import _async_reachable
from custom_components.cisco_meraki.const import DEFAULT_SCAN_INTERVAL

from .conftest import ADMIN_EMAIL, make_api_error

CAMERA = "camera.mv12"
RTSP_SWITCH = "switch.mv12_rtsp_stream"
SERIAL = "Q2BB-BBBB-BBBB"
RTSP_URL = "rtsp://192.168.1.50:9000/live"
RTSP_ON = {"externalRtspEnabled": True, "rtspUrl": RTSP_URL}


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


async def _poll(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    freezer.tick(DEFAULT_SCAN_INTERVAL)
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


@pytest.mark.usefixtures("freezer")
async def test_camera_with_rtsp(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """A camera that exposes its RTSP stream streams it and takes frames from it."""
    mock_client.camera.getDeviceCameraVideoSettings.return_value = RTSP_ON
    await _setup(hass, mock_config_entry)

    assert hass.states.get(CAMERA).state != STATE_UNAVAILABLE
    assert hass.states.get(RTSP_SWITCH).state == STATE_ON
    assert await async_get_stream_source(hass, CAMERA) == RTSP_URL

    with (
        patch(
            "custom_components.cisco_meraki.camera._async_reachable",
            AsyncMock(return_value=True),
        ),
        patch(
            "custom_components.cisco_meraki.camera.ffmpeg.async_get_image",
            AsyncMock(return_value=b"jpeg"),
        ) as get_image,
    ):
        image = await async_get_image(hass, CAMERA)
    assert image.content == b"jpeg"
    # The stream often does not get through over UDP.
    assert get_image.await_args.args[1] == f"-rtsp_transport tcp -i {RTSP_URL}"


@pytest.mark.usefixtures("mock_client", "freezer")
async def test_camera_without_rtsp(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Without the RTSP stream, the camera is unavailable and nothing turns it on."""
    await _setup(hass, mock_config_entry)
    assert hass.states.get(CAMERA).state == STATE_UNAVAILABLE
    assert hass.states.get(RTSP_SWITCH).state == STATE_OFF


@pytest.mark.usefixtures("freezer")
async def test_turn_rtsp_on_and_off(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """The switch exposes the RTSP stream, and the camera becomes available."""
    await _setup(hass, mock_config_entry)
    update = mock_client.camera.updateDeviceCameraVideoSettings
    update.return_value = RTSP_ON

    await hass.services.async_call(
        SWITCH_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: RTSP_SWITCH}, blocking=True
    )
    update.assert_awaited_once_with(SERIAL, externalRtspEnabled=True)
    assert hass.states.get(RTSP_SWITCH).state == STATE_ON
    assert hass.states.get(CAMERA).state != STATE_UNAVAILABLE

    update.reset_mock()
    update.return_value = {"externalRtspEnabled": False}
    await hass.services.async_call(
        SWITCH_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: RTSP_SWITCH}, blocking=True
    )
    update.assert_awaited_once_with(SERIAL, externalRtspEnabled=False)
    assert hass.states.get(CAMERA).state == STATE_UNAVAILABLE


@pytest.mark.parametrize(
    ("status", "message"),
    [
        pytest.param(403, "read-only", id="not_permitted"),
        pytest.param(400, "Could not change the RTSP stream of MV12", id="failed"),
    ],
)
@pytest.mark.usefixtures("freezer")
async def test_turn_rtsp_on_error(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    status: int,
    message: str,
) -> None:
    """A refused change is reported and the switch stays as it was."""
    await _setup(hass, mock_config_entry)
    mock_client.camera.updateDeviceCameraVideoSettings = AsyncMock(
        side_effect=make_api_error(status)
    )
    with pytest.raises(HomeAssistantError, match=message):
        await hass.services.async_call(
            SWITCH_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: RTSP_SWITCH}, blocking=True
        )
    assert hass.states.get(RTSP_SWITCH).state == STATE_OFF


async def test_settings_fetched_every_five_minutes(
    hass: HomeAssistant,
    mock_client: MagicMock,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """RTSP changes made in the Dashboard show up within five minutes."""
    await _setup(hass, mock_config_entry)
    get_settings = mock_client.camera.getDeviceCameraVideoSettings
    get_settings.return_value = RTSP_ON

    await _poll(hass, freezer)
    assert hass.states.get(RTSP_SWITCH).state == STATE_OFF

    freezer.tick(timedelta(minutes=4))
    await _poll(hass, freezer)
    assert hass.states.get(RTSP_SWITCH).state == STATE_ON
    assert get_settings.await_count == 2


@pytest.mark.usefixtures("freezer")
async def test_read_only_key(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """A read-only key gets the camera but no RTSP switch."""
    mock_client.organizations.getOrganizationAdmins.return_value = [
        {"email": ADMIN_EMAIL, "orgAccess": "read-only", "networks": [], "tags": []}
    ]
    mock_client.camera.getDeviceCameraVideoSettings.return_value = RTSP_ON
    await _setup(hass, mock_config_entry)
    assert hass.states.get(CAMERA).state != STATE_UNAVAILABLE
    assert hass.states.get(RTSP_SWITCH) is None


@pytest.mark.usefixtures("freezer")
async def test_unreachable_camera(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """A camera Home Assistant cannot reach gives no image, without running ffmpeg."""
    mock_client.camera.getDeviceCameraVideoSettings.return_value = RTSP_ON
    await _setup(hass, mock_config_entry)

    with (
        patch(
            "custom_components.cisco_meraki.camera.asyncio.open_connection",
            AsyncMock(side_effect=OSError("No route to host")),
        ) as open_connection,
        patch(
            "custom_components.cisco_meraki.camera.ffmpeg.async_get_image"
        ) as get_image,
        pytest.raises(HomeAssistantError),
    ):
        await async_get_image(hass, CAMERA)
    open_connection.assert_awaited_once_with("192.168.1.50", 9000)
    get_image.assert_not_called()


async def test_reachable() -> None:
    """A port that accepts connections counts as reachable."""
    writer = MagicMock(wait_closed=AsyncMock())
    with patch(
        "custom_components.cisco_meraki.camera.asyncio.open_connection",
        AsyncMock(return_value=(MagicMock(), writer)),
    ):
        assert await _async_reachable(RTSP_URL)
    writer.close.assert_called_once()

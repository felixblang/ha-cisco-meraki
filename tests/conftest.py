"""Fixtures for the Meraki Dashboard tests."""

from __future__ import annotations

from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_API_KEY
from meraki import APIError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hass_meraki.const import CONF_ORGANIZATION_ID, DOMAIN

API_KEY = "0123456789abcdef"
ORG_ID = "123456"
ORG_NAME = "Home Lab"
NETWORK_ID = "N_1"

DEVICES = [
    {
        "serial": "Q2AA-AAAA-AAAA",
        "name": "Office AP",
        "model": "MR36",
        "mac": "aa:bb:cc:00:00:01",
        "firmware": "wireless-31-1-6",
        "productType": "wireless",
        "networkId": NETWORK_ID,
        "url": "https://n1.meraki.com/office-ap",
    },
    {
        "serial": "Q2BB-BBBB-BBBB",
        "name": "",
        "model": "MV12",
        "mac": "aa:bb:cc:00:00:02",
        "firmware": "camera-5-2",
        "productType": "camera",
        "networkId": NETWORK_ID,
    },
]

AVAILABILITIES = [
    {"serial": "Q2AA-AAAA-AAAA", "status": "online"},
    {"serial": "Q2BB-BBBB-BBBB", "status": "offline"},
]

CLIENTS = [
    {"status": "Online", "recentDeviceSerial": "Q2AA-AAAA-AAAA"},
    {"status": "Online", "recentDeviceSerial": "Q2AA-AAAA-AAAA"},
    {"status": "Offline", "recentDeviceSerial": "Q2AA-AAAA-AAAA"},
]


def make_api_error(status: int) -> APIError:
    """Build an APIError like the SDK raises it."""
    response = MagicMock(status_code=status, reason_phrase="error")
    response.json.return_value = {"errors": ["error"]}
    return APIError({"tags": ["test"], "operation": "test"}, response)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: Any) -> None:
    """Enable loading the custom integration in every test."""


@pytest.fixture
def mock_client() -> Generator[MagicMock]:
    """Patch the Meraki SDK client."""
    client = MagicMock()
    client.__aexit__ = AsyncMock()
    client.organizations.getOrganizations = AsyncMock(
        return_value=[{"id": ORG_ID, "name": ORG_NAME}]
    )
    client.organizations.getOrganizationNetworks = AsyncMock(
        return_value=[{"id": NETWORK_ID, "name": "Home"}, {"id": "N_2", "name": "Lab"}]
    )
    client.organizations.getOrganizationDevices = AsyncMock(
        return_value=[dict(d) for d in DEVICES]
    )
    client.organizations.getOrganizationDevicesAvailabilities = AsyncMock(
        return_value=list(AVAILABILITIES)
    )
    client.networks.getNetworkClients = AsyncMock(return_value=list(CLIENTS))
    with patch(
        "custom_components.hass_meraki.api.AsyncDashboardAPI", return_value=client
    ):
        yield client


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Return a config entry for the test organization."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=ORG_NAME,
        unique_id=ORG_ID,
        data={CONF_API_KEY: API_KEY, CONF_ORGANIZATION_ID: ORG_ID},
    )

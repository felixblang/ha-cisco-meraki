"""Fixtures for the Cisco Meraki tests."""

from __future__ import annotations

from collections.abc import Generator
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_API_KEY
from meraki import APIError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.cisco_meraki.const import CONF_ORGANIZATION_ID, DOMAIN

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
    {
        "serial": "Q2SW-SSSS-SSSS",
        "name": "Core Switch",
        "model": "MS120-8",
        "mac": "aa:bb:cc:00:00:03",
        "productType": "switch",
        "networkId": NETWORK_ID,
    },
    {
        "serial": "Q2MX-MMMM-MMMM",
        "name": "Gateway",
        "model": "MX67",
        "mac": "aa:bb:cc:00:00:04",
        "productType": "appliance",
        "networkId": NETWORK_ID,
    },
]


def _quality_points(
    start: datetime, latencies: list[float | None], loss: float
) -> list[dict[str, Any]]:
    return [
        {
            "ts": (start + timedelta(minutes=i)).isoformat().replace("+00:00", "Z"),
            "latencyMs": latency,
            "lossPercent": loss,
        }
        for i, latency in enumerate(latencies)
    ]


QUALITY_START = datetime(2026, 10, 20, 9, 0, 25, tzinfo=UTC)

# WAN 1 is probed over IPv4 and IPv6; WAN 2 is down and loses every probe.
UPLINK_LOSS_AND_LATENCY = [
    {
        "serial": "Q2MX-MMMM-MMMM",
        "uplink": "wan1",
        "ip": "2620:12f:c00c::a",
        "timeSeries": _quality_points(QUALITY_START, [99.0] * 7, 50.0),
    },
    {
        "serial": "Q2MX-MMMM-MMMM",
        "uplink": "wan1",
        "ip": "8.8.8.8",
        "timeSeries": _quality_points(QUALITY_START, [20, 22, 24, 26, 28, 30, 32], 0.0),
    },
    {
        "serial": "Q2MX-MMMM-MMMM",
        "uplink": "wan2",
        "ip": "8.8.8.8",
        "timeSeries": _quality_points(QUALITY_START, [None] * 7, 100.0),
    },
]

UPLINK_STATUSES = [
    {
        "serial": "Q2MX-MMMM-MMMM",
        "model": "MX67",
        "networkId": NETWORK_ID,
        "uplinks": [
            {
                "interface": "wan1",
                "status": "active",
                "ip": "192.0.2.10",
                "gateway": "192.0.2.1",
                "publicIp": "198.51.100.7",
                "primaryDns": "192.0.2.1",
                "secondaryDns": None,
                "ipAssignedBy": "dhcp",
            },
            {"interface": "wan2", "status": "not connected"},
            {"interface": "cellular", "status": "ready"},
        ],
    },
    # Devices outside the configured networks are ignored.
    {
        "serial": "Q2XX-XXXX-XXXX",
        "uplinks": [{"interface": "wan1", "status": "active"}],
    },
]

SWITCH_PORTS = [
    {
        "serial": "Q2SW-SSSS-SSSS",
        "ports": [
            {
                "portId": "1",
                "name": "Desk",
                "enabled": True,
                "type": "access",
                "vlan": 1,
                "poeEnabled": True,
            },
            {
                "portId": "2",
                "name": "",
                "enabled": False,
                "type": "access",
                "vlan": 1,
                "poeEnabled": True,
            },
            {
                "portId": "8",
                "name": "Uplink",
                "enabled": True,
                "type": "trunk",
                "vlan": 1,
            },
        ],
    },
    # A switch outside the configured networks must be ignored.
    {"serial": "Q2XX-XXXX-XXXX", "ports": [{"portId": "1", "enabled": True}]},
]

COPPER_CAPABILITIES = [
    "Auto negotiate",
    "1 Gigabit full duplex (auto)",
    "100 Megabit (auto)",
]
SFP_CAPABILITIES = ["Auto negotiate", "1 Gigabit full duplex (forced)"]

# Per-switch port details; only used to tell RJ45 from SFP ports.
DEVICE_SWITCH_PORTS = [
    {"portId": "1", "linkNegotiationCapabilities": COPPER_CAPABILITIES},
    {"portId": "2", "linkNegotiationCapabilities": COPPER_CAPABILITIES},
    {"portId": "8", "linkNegotiationCapabilities": SFP_CAPABILITIES},
]

SWITCH_PORT_STATUSES = {
    "items": [
        {
            "serial": "Q2SW-SSSS-SSSS",
            "ports": [
                {
                    "portId": "1",
                    "status": "Connected",
                    "isUplink": False,
                    "speed": "1 Gbps",
                    "duplex": "full",
                },
                {
                    "portId": "2",
                    "status": "Disconnected",
                    "isUplink": False,
                    "speed": "",
                    "duplex": "",
                    "poe": {"isAllocated": True},
                },
                {
                    "portId": "8",
                    "status": "Connected",
                    "isUplink": True,
                    "speed": "1 Gbps",
                    "duplex": "full",
                },
            ],
        }
    ],
    "meta": {"counts": {"items": {"total": 1, "remaining": 0}}},
}

AVAILABILITIES = [
    {"serial": "Q2AA-AAAA-AAAA", "status": "online"},
    {"serial": "Q2BB-BBBB-BBBB", "status": "offline"},
    {"serial": "Q2SW-SSSS-SSSS", "status": "online"},
]

CLIENTS = [
    {"status": "Online", "recentDeviceSerial": "Q2AA-AAAA-AAAA"},
    {"status": "Online", "recentDeviceSerial": "Q2AA-AAAA-AAAA"},
    {"status": "Offline", "recentDeviceSerial": "Q2AA-AAAA-AAAA"},
    {
        "status": "Online",
        "recentDeviceSerial": "Q2SW-SSSS-SSSS",
        "switchport": "1",
        "description": "office-pc",
    },
    {
        "status": "Online",
        "recentDeviceSerial": "Q2SW-SSSS-SSSS",
        "switchport": "1",
        "description": None,
        "mac": "aa:bb:cc:dd:ee:ff",
    },
    # Seen on port 1 earlier in the hour, but unplugged since.
    {
        "status": "Offline",
        "recentDeviceSerial": "Q2SW-SSSS-SSSS",
        "switchport": "1",
        "description": "unplugged-pc",
    },
]


def _usage(start: datetime, wh: float | None) -> dict[str, Any]:
    end = start + timedelta(minutes=5)
    return {
        "startTs": start.isoformat().replace("+00:00", "Z"),
        "endTs": end.isoformat().replace("+00:00", "Z"),
        "energy": {"usage": None if wh is None else {"total": wh}},
    }


USAGE_START = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)

# Port 2 powers a PoE device that has no network link. Port 1 has PoE enabled but
# nothing draws power from it, and port 8 has no PoE at all.
SWITCH_PORT_USAGE = {
    "items": [
        {
            "serial": "Q2SW-SSSS-SSSS",
            "ports": [
                {"portId": "1", "intervals": [_usage(USAGE_START, 0.0)]},
                {
                    "portId": "2",
                    "intervals": [
                        _usage(USAGE_START + timedelta(minutes=5 * i), 0.1)
                        for i in range(14)
                    ],
                },
                {"portId": "8", "intervals": [_usage(USAGE_START, None)]},
            ],
        }
    ],
    "meta": {"counts": {"items": {"total": 1, "remaining": 0}}},
}

SWITCH_PORT_TOPOLOGY = {
    "items": [
        {
            "serial": "Q2SW-SSSS-SSSS",
            "ports": [
                {
                    "portId": "8",
                    "lastUpdatedAt": "2026-10-20T08:00:00Z",
                    "lldp": [
                        {"name": "System name", "value": "Router"},
                        {"name": "Chassis ID", "value": "aa:aa:aa:aa:aa:aa"},
                    ],
                    "cdp": [],
                },
                {
                    "portId": "1",
                    "lastUpdatedAt": "2026-10-20T08:00:00Z",
                    "lldp": [{"name": "Chassis ID", "value": "bb:bb:bb:bb:bb:bb"}],
                    "cdp": [],
                },
                # Port 2 has no link; Meraki still reports the device that was on it.
                {
                    "portId": "2",
                    "lastUpdatedAt": "2026-10-20T08:00:00Z",
                    "lldp": [{"name": "System name", "value": "Old device"}],
                    "cdp": [],
                },
            ],
        }
    ],
    "meta": {"counts": {"items": {"total": 1, "remaining": 0}}},
}


# The MV12's analytics zones: the full frame and a drawn zone.
CAMERA_ZONES = [
    {"zoneId": "0", "type": "occupancy", "label": "Full Frame"},
    {"id": "619244948763443982", "type": "area", "name": "Entrance"},
]


def make_api_error(status: int) -> APIError:
    """Build an APIError like the SDK raises it."""
    response = MagicMock(status_code=status, reason_phrase="error")
    response.json.return_value = {"errors": ["error"]}
    return APIError({"tags": ["test"], "operation": "test"}, response)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    recorder_mock: Any, enable_custom_integrations: Any
) -> None:
    """Load the custom integration, and the recorder it depends on, in every test."""


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
    client.organizations.getOrganizationUplinksStatuses = AsyncMock(
        return_value=deepcopy(UPLINK_STATUSES)
    )
    client.organizations.getOrganizationDevicesUplinksLossAndLatency = AsyncMock(
        return_value=deepcopy(UPLINK_LOSS_AND_LATENCY)
    )
    client.switch.getOrganizationSwitchPortsBySwitch = AsyncMock(
        return_value=deepcopy(SWITCH_PORTS)
    )
    client.switch.getOrganizationSwitchPortsStatusesBySwitch = AsyncMock(
        return_value=deepcopy(SWITCH_PORT_STATUSES)
    )
    client.switch.getOrganizationSwitchPortsTopologyDiscoveryByDevice = AsyncMock(
        return_value=deepcopy(SWITCH_PORT_TOPOLOGY)
    )
    client.switch.getDeviceSwitchPorts = AsyncMock(
        return_value=deepcopy(DEVICE_SWITCH_PORTS)
    )
    client.switch.getOrganizationSwitchPortsUsageHistoryByDeviceByInterval = AsyncMock(
        return_value=deepcopy(SWITCH_PORT_USAGE)
    )
    client.switch.updateDeviceSwitchPort = AsyncMock(return_value={})
    client.camera.getDeviceCameraAnalyticsZones = AsyncMock(
        return_value=deepcopy(CAMERA_ZONES)
    )
    # No analytics through the API unless a test sets them.
    client.camera.getDeviceCameraAnalyticsRecent = AsyncMock(return_value=[])
    with patch(
        "custom_components.cisco_meraki.api.AsyncDashboardAPI", return_value=client
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

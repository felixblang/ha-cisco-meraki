"""Data update coordinator for the Meraki Dashboard integration."""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from meraki import APIError

from .api import is_auth_error
from .const import (
    CLIENT_PRODUCT_TYPES,
    CLIENT_TIMESPAN_SECONDS,
    CONF_NETWORKS,
    CONF_ORGANIZATION_ID,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    LOGGER,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from meraki.aio import AsyncDashboardAPI

type MerakiConfigEntry = ConfigEntry[MerakiDataUpdateCoordinator]


@dataclass(slots=True)
class MerakiDevice:
    """A Meraki device and its current state."""

    serial: str
    name: str
    model: str | None
    mac: str | None
    firmware: str | None
    product_type: str | None
    network_id: str | None
    url: str | None
    status: str | None
    client_count: int = 0


@dataclass(slots=True)
class MerakiData:
    """Everything fetched for one organization in a single update."""

    devices: dict[str, MerakiDevice] = field(default_factory=dict)


class MerakiDataUpdateCoordinator(DataUpdateCoordinator[MerakiData]):
    """Poll a Meraki organization for devices, availability and client counts."""

    config_entry: MerakiConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: MerakiConfigEntry,
        client: AsyncDashboardAPI,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=DEFAULT_SCAN_INTERVAL,
        )
        self.client = client
        self.organization_id: str = config_entry.data[CONF_ORGANIZATION_ID]

    async def _async_update_data(self) -> MerakiData:
        """Fetch the latest data from the Meraki Dashboard API."""
        try:
            return await self._async_fetch()
        except APIError as err:
            if is_auth_error(err):
                raise ConfigEntryAuthFailed(
                    translation_domain=DOMAIN,
                    translation_key="invalid_auth",
                ) from err
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="update_failed",
                translation_placeholders={"error": str(err)},
            ) from err

    async def _async_fetch(self) -> MerakiData:
        org_api = self.client.organizations
        devices, availabilities = await asyncio.gather(
            org_api.getOrganizationDevices(self.organization_id, total_pages="all"),
            org_api.getOrganizationDevicesAvailabilities(
                self.organization_id, total_pages="all"
            ),
        )

        network_filter = set(self.config_entry.options.get(CONF_NETWORKS) or [])
        if network_filter:
            devices = [d for d in devices if d.get("networkId") in network_filter]

        status_by_serial = {
            item["serial"]: item.get("status") for item in availabilities
        }

        data = MerakiData()
        for device in devices:
            serial = device.get("serial")
            if not serial:
                continue
            data.devices[serial] = MerakiDevice(
                serial=serial,
                name=device.get("name") or device.get("model") or serial,
                model=device.get("model"),
                mac=device.get("mac"),
                firmware=device.get("firmware"),
                product_type=device.get("productType"),
                network_id=device.get("networkId"),
                url=device.get("url"),
                status=status_by_serial.get(serial),
            )

        # Only networks that contain a client-serving device need a client query.
        network_ids = {
            d.network_id
            for d in data.devices.values()
            if d.network_id and d.product_type in CLIENT_PRODUCT_TYPES
        }
        client_lists = await asyncio.gather(
            *(self._async_fetch_network_clients(nid) for nid in network_ids)
        )
        counts: Counter[str] = Counter(
            client["recentDeviceSerial"]
            for clients in client_lists
            for client in clients
            if client.get("status") == "Online" and client.get("recentDeviceSerial")
        )
        for serial, count in counts.items():
            if serial in data.devices:
                data.devices[serial].client_count = count

        return data

    async def _async_fetch_network_clients(
        self, network_id: str
    ) -> list[dict[str, Any]]:
        return await self.client.networks.getNetworkClients(
            network_id,
            total_pages="all",
            perPage=5000,
            timespan=CLIENT_TIMESPAN_SECONDS,
        )

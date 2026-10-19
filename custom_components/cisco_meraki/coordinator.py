"""Data update coordinator for the Cisco Meraki integration."""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from homeassistant.core import CALLBACK_TYPE, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
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
    PORT_CHANGE_REFRESH_DELAYS,
    PORT_USAGE_MAX_BACKFILL,
    PORT_USAGE_MIN_SPAN,
)
from .stats import (
    HOUR,
    SHORT_TERM,
    MerakiStatistic,
    StatisticKind,
    StatisticsWriter,
    statistic_id,
)

if TYPE_CHECKING:
    from datetime import datetime

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from meraki.aio import AsyncDashboardAPI

    from .sensor_coordinator import MerakiSensorCoordinator

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
class MerakiSwitchPort:
    """A switch port's configuration and link state."""

    port_id: str
    name: str | None
    enabled: bool
    type: str | None
    vlan: int | None
    is_uplink: bool = False
    status: str | None = None
    speed: str | None = None
    duplex: str | None = None
    # LLDP system name (or CDP device ID) of the device on the other end.
    neighbor: str | None = None
    # Physical connector, "RJ45" or "SFP".
    connector: str | None = None
    # Names of wired clients seen on the port during the client look-back window.
    clients: list[str] = field(default_factory=list)
    # PoE is enabled in the port's configuration.
    poe_enabled: bool = False
    # The port is currently delivering PoE power.
    poe_active: bool = False
    # Average PoE power (W) during the newest reported interval.
    poe_power: float | None = None


@dataclass(slots=True)
class MerakiData:
    """Everything fetched for one organization in a single update."""

    devices: dict[str, MerakiDevice] = field(default_factory=dict)
    # Switch serial -> port ID -> port.
    ports: dict[str, dict[str, MerakiSwitchPort]] = field(default_factory=dict)


class MerakiDataUpdateCoordinator(DataUpdateCoordinator[MerakiData]):
    """Poll a Meraki organization for devices, client counts and switch ports."""

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
        self._port_refresh_unsubs: list[CALLBACK_TYPE] = []
        # Switch serial -> port ID -> connector. Hardware doesn't change, so each
        # switch is looked up once.
        self._port_connectors: dict[str, dict[str, str | None]] = {}
        # PoE energy statistics per switch port.
        self.port_energy = StatisticsWriter(hass)
        config_entry.async_on_unload(self._async_cancel_port_refreshes)
        # Polls MT sensor readings, if the organization has sensors.
        self.sensors: MerakiSensorCoordinator | None = None

    @property
    def sensors_present(self) -> bool:
        """Return True if the organization has MT sensors."""
        return any(d.product_type == "sensor" for d in self.data.devices.values())

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

        if any(d.product_type == "switch" for d in data.devices.values()):
            await self._async_fetch_switch_ports(data)
            _add_port_clients(data, client_lists)

        return data

    async def _async_fetch_switch_ports(self, data: MerakiData) -> None:
        """Add port configuration, link status and neighbors for every switch."""
        switch_api = self.client.switch
        configs, statuses, topology = await asyncio.gather(
            switch_api.getOrganizationSwitchPortsBySwitch(
                self.organization_id, total_pages="all", perPage=50
            ),
            switch_api.getOrganizationSwitchPortsStatusesBySwitch(
                self.organization_id, total_pages="all", perPage=20
            ),
            self._async_fetch_topology(),
        )

        for switch in configs:
            serial = switch.get("serial")
            if serial not in data.devices:
                continue
            data.ports[serial] = {
                port["portId"]: MerakiSwitchPort(
                    port_id=port["portId"],
                    name=port.get("name") or None,
                    enabled=bool(port.get("enabled")),
                    type=port.get("type"),
                    vlan=port.get("vlan"),
                    poe_enabled=bool(port.get("poeEnabled")),
                )
                for port in switch.get("ports", [])
            }

        for switch in _unwrap_items(statuses):
            ports = data.ports.get(switch.get("serial"))
            if not ports:
                continue
            for status in switch.get("ports", []):
                if port := ports.get(status.get("portId")):
                    port.is_uplink = bool(status.get("isUplink"))
                    port.status = status.get("status")
                    port.speed = status.get("speed") or None
                    port.duplex = status.get("duplex") or None
                    port.poe_active = bool((status.get("poe") or {}).get("isAllocated"))

        for switch in topology:
            ports = data.ports.get(switch.get("serial"))
            if not ports:
                continue
            for discovered in switch.get("ports", []):
                if port := ports.get(discovered.get("portId")):
                    port.neighbor = _neighbor_name(discovered)

        await self._async_add_port_connectors(data)
        await self._async_add_port_energy(data)

    async def _async_add_port_energy(self, data: MerakiData) -> None:
        """Add PoE energy statistics and the current PoE power of each port."""
        now = dt_util.utcnow()
        since = await self._async_port_energy_since(data, now)
        get_usage = (
            self.client.switch.getOrganizationSwitchPortsUsageHistoryByDeviceByInterval
        )
        try:
            usage = await get_usage(
                self.organization_id,
                total_pages="all",
                perPage=50,
                interval=int(SHORT_TERM.total_seconds()),
                # Shorter spans return no data at all.
                timespan=int(max(now - since, PORT_USAGE_MIN_SPAN).total_seconds()),
            )
        except APIError as err:
            if is_auth_error(err):
                raise
            LOGGER.debug("Could not fetch port energy usage: %s", err)
            return

        for switch in _unwrap_items(usage):
            serial = switch.get("serial")
            ports = data.ports.get(serial)
            if not ports:
                continue
            for usage_port in switch.get("ports", []):
                port = ports.get(usage_port.get("portId"))
                if port is None:
                    continue
                intervals = sorted(
                    (ts, wh)
                    for interval in usage_port.get("intervals", [])
                    if (ts := dt_util.parse_datetime(interval.get("startTs") or ""))
                    and (wh := _interval_energy(interval)) is not None
                )
                if not intervals:
                    continue
                stat = await self._async_port_energy_statistic(data, serial, port)
                # PoE ports that have never delivered power would only add sensors
                # stuck at zero; they are picked up once something draws power.
                if (
                    stat.watermark is None
                    and not stat.hour_sum
                    and not any(wh for _, wh in intervals)
                ):
                    continue
                for ts, wh in intervals:
                    stat.add(dt_util.as_utc(ts), wh)
                port.poe_power = intervals[-1][1] * HOUR / SHORT_TERM
        self.port_energy.async_write(now)

    async def _async_port_energy_since(
        self, data: MerakiData, now: datetime
    ) -> datetime:
        """Return the time from which port energy has to be fetched."""
        if not self.port_energy.statistics:
            # First run: continue where the statistics of PoE ports left off.
            for serial, ports in data.ports.items():
                for port in ports.values():
                    if port.poe_enabled:
                        await self._async_port_energy_statistic(data, serial, port)
            resume = [
                stat.hour_done
                for stat in self.port_energy.statistics.values()
                if stat.hour_done is not None
            ]
            start = min(resume, default=now - 2 * HOUR)
        else:
            start = min(
                (
                    stat.watermark
                    for stat in self.port_energy.statistics.values()
                    if stat.watermark is not None
                ),
                default=now - 2 * HOUR,
            )
        return max(start, now - PORT_USAGE_MAX_BACKFILL)

    async def _async_port_energy_statistic(
        self, data: MerakiData, serial: str, port: MerakiSwitchPort
    ) -> MerakiStatistic:
        device = data.devices[serial]
        return await self.port_energy.async_ensure(
            statistic_id(serial, f"port_{port.port_id}_poe_energy"),
            f"{device.name} Port {port.port_id} PoE energy",
            None,
            StatisticKind.ENERGY_DELTA,
        )

    def port_energy_total(self, serial: str, port_id: str) -> float | None:
        """Return the PoE energy (kWh) a port has delivered so far."""
        stat = self.port_energy.statistics.get(
            statistic_id(serial, f"port_{port_id}_poe_energy")
        )
        return stat.energy_total if stat else None

    async def _async_add_port_connectors(self, data: MerakiData) -> None:
        """Set each port's connector, looking up switches not seen before."""
        missing = [
            serial for serial in data.ports if serial not in self._port_connectors
        ]
        results = await asyncio.gather(
            *(self._async_fetch_port_connectors(serial) for serial in missing)
        )
        for serial, connectors in zip(missing, results, strict=True):
            if connectors is not None:
                self._port_connectors[serial] = connectors

        for serial, ports in data.ports.items():
            connectors = self._port_connectors.get(serial, {})
            for port in ports.values():
                port.connector = connectors.get(port.port_id)

    async def _async_fetch_port_connectors(
        self, serial: str
    ) -> dict[str, str | None] | None:
        """Return the connector per port of a switch, or None to retry next poll."""
        # Only the per-switch endpoint reports the speeds a port can negotiate.
        try:
            ports = await self.client.switch.getDeviceSwitchPorts(serial)
        except APIError as err:
            if is_auth_error(err):
                raise
            LOGGER.debug("Could not fetch port details of %s: %s", serial, err)
            return None
        return {
            port["portId"]: _connector(port.get("linkNegotiationCapabilities"))
            for port in ports
        }

    async def _async_fetch_topology(self) -> list[dict[str, Any]]:
        """Return LLDP/CDP neighbors per switch, or nothing if they are unavailable."""
        # Neighbors are a nice-to-have, so a failure here must not fail the update.
        switch_api = self.client.switch
        try:
            topology = (
                await switch_api.getOrganizationSwitchPortsTopologyDiscoveryByDevice(
                    self.organization_id, total_pages="all", perPage=20
                )
            )
        except APIError as err:
            if is_auth_error(err):
                raise
            LOGGER.debug("Could not fetch LLDP/CDP neighbors: %s", err)
            return []
        return _unwrap_items(topology)

    async def async_set_port_enabled(
        self, serial: str, port_id: str, *, enabled: bool
    ) -> None:
        """Enable or disable a switch port."""
        try:
            await self.client.switch.updateDeviceSwitchPort(
                serial, port_id, enabled=enabled
            )
        except APIError as err:
            if is_auth_error(err):
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="write_not_permitted",
                ) from err
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="port_update_failed",
                translation_placeholders={"port": port_id, "error": str(err)},
            ) from err

        # The Dashboard accepts the change at once, but the switch only applies it on
        # its next config sync, usually within a minute or two. Show the new setting
        # now and poll again shortly so the link state follows as soon as it changes.
        self.data.ports[serial][port_id].enabled = enabled
        self.async_update_listeners()
        self._async_schedule_port_refreshes()

    @callback
    def _async_schedule_port_refreshes(self) -> None:
        self._async_cancel_port_refreshes()
        self._port_refresh_unsubs = [
            async_call_later(self.hass, delay, self._async_port_refresh)
            for delay in PORT_CHANGE_REFRESH_DELAYS
        ]

    @callback
    def _async_cancel_port_refreshes(self) -> None:
        for unsub in self._port_refresh_unsubs:
            unsub()
        self._port_refresh_unsubs = []

    async def _async_port_refresh(self, _now: datetime) -> None:
        await self.async_request_refresh()

    async def _async_fetch_network_clients(
        self, network_id: str
    ) -> list[dict[str, Any]]:
        return await self.client.networks.getNetworkClients(
            network_id,
            total_pages="all",
            perPage=5000,
            timespan=CLIENT_TIMESPAN_SECONDS,
        )


def _unwrap_items(response: Any) -> list[dict[str, Any]]:
    """Return the list of an endpoint that wraps its results in "items"."""
    if isinstance(response, dict):
        return response.get("items", [])
    return response


def _neighbor_name(discovered: dict[str, Any]) -> str | None:
    """Return the most descriptive name of a port's LLDP or CDP neighbor."""
    # The topology endpoint reports each protocol as a list of name/value pairs.
    lldp = {
        f["name"]: f["value"] for f in discovered.get("lldp") or [] if f.get("value")
    }
    cdp = {f["name"]: f["value"] for f in discovered.get("cdp") or [] if f.get("value")}
    return (
        lldp.get("System name")
        or cdp.get("Device ID")
        or lldp.get("Chassis ID")
        or cdp.get("Platform")
    )


def _add_port_clients(
    data: MerakiData, client_lists: list[list[dict[str, Any]]]
) -> None:
    """Attach the wired clients each switch port has seen."""
    for clients in client_lists:
        for client in clients:
            ports = data.ports.get(client.get("recentDeviceSerial"))
            port_id = client.get("switchport")
            if not ports or not port_id or port_id not in ports:
                continue
            ports[port_id].clients.append(
                client.get("description")
                or client.get("ip")
                or client.get("mac")
                or "?"
            )
    for ports in data.ports.values():
        for port in ports.values():
            port.clients.sort(key=str.lower)


def _connector(capabilities: list[str] | None) -> str | None:
    """Tell copper from fiber ports by the speeds they can negotiate."""
    if not capabilities:
        return None
    # 10 and 100 Mbit/s are copper speeds; SFP ports only offer gigabit and above.
    if any("Megabit" in capability for capability in capabilities):
        return "RJ45"
    return "SFP"


def _interval_energy(interval: dict[str, Any]) -> float | None:
    """Return the energy (Wh) of a port usage interval, if it has any."""
    usage = (interval.get("energy") or {}).get("usage") or {}
    total = usage.get("total")
    return float(total) if isinstance(total, int | float) else None

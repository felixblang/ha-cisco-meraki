"""Data update coordinator for the Cisco Meraki integration."""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from homeassistant.const import PERCENTAGE, UnitOfPower, UnitOfTime
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
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
    PORT_ENERGY_INTERVAL,
    PORT_USAGE_MAX_BACKFILL,
    PORT_USAGE_MIN_SPAN,
    UPLINK_PRODUCT_TYPES,
    UPLINK_QUALITY_DELAY,
    UPLINK_QUALITY_MAX_BACKFILL,
    UPLINK_QUALITY_WINDOW,
    UPLINK_STATUSES,
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

    from .camera_sense import MerakiCameraSense
    from .sensor_coordinator import MerakiSensorCoordinator

type MerakiConfigEntry = ConfigEntry[MerakiDataUpdateCoordinator]

# An energy interval's Wh, divided by its length in hours, is its average power.
WATT_HOURS_TO_WATTS = HOUR / SHORT_TERM
# A longer gap between port usage intervals is taken as the switch not reporting,
# not as intervals Meraki merged into one.
MAX_MERGED_INTERVAL = 3 * SHORT_TERM


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
    # Names of the wired clients currently online on the port.
    clients: list[str] = field(default_factory=list)
    # PoE is enabled in the port's configuration.
    poe_enabled: bool = False
    # The port is currently delivering PoE power.
    poe_active: bool = False
    # Average PoE power (W) during the newest reported interval.
    poe_power: float | None = None


@dataclass(slots=True)
class MerakiUplink:
    """A WAN uplink of an appliance or gateway."""

    interface: str
    status: str | None
    ip: str | None = None
    gateway: str | None = None
    public_ip: str | None = None
    primary_dns: str | None = None
    secondary_dns: str | None = None
    ip_assigned_by: str | None = None


@dataclass(slots=True)
class MerakiUplinkQuality:
    """The newest loss and latency measurement of an uplink."""

    ts: datetime
    destination: str
    latency: float | None
    loss: float | None


@dataclass(slots=True)
class MerakiData:
    """Everything fetched for one organization in a single update."""

    devices: dict[str, MerakiDevice] = field(default_factory=dict)
    # Switch serial -> port ID -> port.
    ports: dict[str, dict[str, MerakiSwitchPort]] = field(default_factory=dict)
    # Appliance serial -> interface (e.g. "wan1") -> uplink.
    uplinks: dict[str, dict[str, MerakiUplink]] = field(default_factory=dict)
    # Switch serial -> total PoE power (W) of the newest energy interval.
    switch_poe_power: dict[str, float] = field(default_factory=dict)
    # (Appliance serial, interface) -> newest loss and latency measurement.
    uplink_quality: dict[tuple[str, str], MerakiUplinkQuality] = field(
        default_factory=dict
    )


class MerakiDataUpdateCoordinator(DataUpdateCoordinator[MerakiData]):
    """Poll a Meraki organization for devices, switch ports and uplinks."""

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
        # Switch serial -> port ID -> connector. Hardware doesn't change, so each
        # switch is looked up once.
        self._port_connectors: dict[str, dict[str, str | None]] = {}
        # PoE energy statistics per switch port.
        self.port_energy = StatisticsWriter(hass)
        self._port_energy_fetched_at: datetime | None = None
        # (Switch serial, port ID) -> PoE power of the newest energy interval.
        self._poe_power: dict[tuple[str, str], float] = {}
        # Switch serial -> total PoE power of the newest energy interval.
        self._switch_poe_power: dict[str, float] = {}
        # PoE power per port and per switch, at the times the intervals were measured.
        self.poe_power_stats = StatisticsWriter(hass)
        # (Switch serial, port ID) -> last link status seen, and since when the
        # current link has been up, to tell outdated LLDP/CDP neighbors.
        self._link_status: dict[tuple[str, str], str | None] = {}
        self._link_up_since: dict[tuple[str, str], datetime] = {}
        # Uplink loss and latency, measured every minute, at their own timestamps.
        self.uplink_stats = StatisticsWriter(hass)
        self._uplink_quality: dict[tuple[str, str], MerakiUplinkQuality] = {}
        # Polls MT sensor readings, if the organization has sensors.
        self.sensors: MerakiSensorCoordinator | None = None
        # MV Sense data of the organization's cameras, received over MQTT.
        self.camera_sense: MerakiCameraSense | None = None

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

        if any(d.product_type in UPLINK_PRODUCT_TYPES for d in data.devices.values()):
            await self._async_fetch_uplinks(data)
            await self._async_fetch_uplink_quality(data)

        if self.camera_sense:
            await self.camera_sense.async_poll(self.client)

        return data

    async def _async_fetch_uplink_quality(self, data: MerakiData) -> None:
        """Add uplink loss and latency, and write their statistics."""
        now = dt_util.utcnow()
        end = (now - UPLINK_QUALITY_DELAY).replace(microsecond=0)
        start = await self._async_uplink_quality_since(data, end)
        get_quality = (
            self.client.organizations.getOrganizationDevicesUplinksLossAndLatency
        )
        try:
            while start < end:
                window_end = min(start + UPLINK_QUALITY_WINDOW, end)
                series = await get_quality(
                    self.organization_id,
                    t0=start.isoformat(),
                    t1=window_end.isoformat(),
                )
                await self._async_add_uplink_quality(data, series)
                start = window_end
        except APIError as err:
            if is_auth_error(err):
                raise
            # Loss and latency are a nice-to-have; keep the uplink status.
            LOGGER.debug("Could not fetch uplink loss and latency: %s", err)
        finally:
            data.uplink_quality = dict(self._uplink_quality)
            await self.uplink_stats.async_refresh_and_write()

    async def _async_uplink_quality_since(
        self, data: MerakiData, end: datetime
    ) -> datetime:
        """Return the time from which uplink loss and latency have to be fetched."""
        earliest = end - UPLINK_QUALITY_MAX_BACKFILL
        if not self.uplink_stats.statistics:
            # First run: continue where the uplinks' statistics left off.
            for serial, uplinks in data.uplinks.items():
                for interface in uplinks:
                    await self._async_uplink_quality_statistics(data, serial, interface)
            resume = [
                stat.hour_done
                for stat in self.uplink_stats.statistics.values()
                if stat.hour_done is not None
            ]
            return max(min(resume, default=end - UPLINK_QUALITY_WINDOW), earliest)
        watermarks = [
            stat.watermark
            for stat in self.uplink_stats.statistics.values()
            if stat.watermark is not None
        ]
        return max(min(watermarks, default=end - UPLINK_QUALITY_WINDOW), earliest)

    async def _async_add_uplink_quality(
        self, data: MerakiData, series: list[dict[str, Any]]
    ) -> None:
        # Meraki probes several destinations per uplink; use one, preferring IPv4.
        chosen: dict[tuple[str, str], dict[str, Any]] = {}
        for item in sorted(
            series, key=lambda i: (":" in (i.get("ip") or ""), i.get("ip") or "")
        ):
            key = (item.get("serial", ""), item.get("uplink", ""))
            if key[0] in data.devices and key[1]:
                chosen.setdefault(key, item)

        for (serial, interface), item in chosen.items():
            latency_stat, loss_stat = await self._async_uplink_quality_statistics(
                data, serial, interface
            )
            points = sorted(
                (
                    (dt_util.as_utc(ts), point)
                    for point in item.get("timeSeries", [])
                    if (ts := dt_util.parse_datetime(point.get("ts") or ""))
                ),
                key=lambda pair: pair[0],
            )
            for ts, point in points:
                latency, loss = point.get("latencyMs"), point.get("lossPercent")
                if latency is not None:
                    latency_stat.add(ts, float(latency))
                if loss is not None:
                    loss_stat.add(ts, float(loss))
            if points:
                ts, point = points[-1]
                self._uplink_quality[(serial, interface)] = MerakiUplinkQuality(
                    ts=ts,
                    destination=item.get("ip") or "",
                    latency=point.get("latencyMs"),
                    loss=point.get("lossPercent"),
                )

    async def _async_uplink_quality_statistics(
        self, data: MerakiData, serial: str, interface: str
    ) -> tuple[MerakiStatistic, MerakiStatistic]:
        """Return the latency and loss statistics of an uplink."""
        device = data.devices[serial]
        return (
            await self.uplink_stats.async_ensure(
                statistic_id(serial, f"uplink_{interface}_latency"),
                f"{device.name} {interface} latency",
                UnitOfTime.MILLISECONDS,
                StatisticKind.MEAN,
                entity_unique_id=f"{serial}_uplink_{interface}_latency",
            ),
            await self.uplink_stats.async_ensure(
                statistic_id(serial, f"uplink_{interface}_loss"),
                f"{device.name} {interface} packet loss",
                PERCENTAGE,
                StatisticKind.MEAN,
                entity_unique_id=f"{serial}_uplink_{interface}_loss",
            ),
        )

    async def _async_fetch_uplinks(self, data: MerakiData) -> None:
        """Add the WAN uplinks of appliances and gateways."""
        statuses = await self.client.organizations.getOrganizationUplinksStatuses(
            self.organization_id, total_pages="all"
        )
        for device in statuses:
            serial = device.get("serial")
            if serial not in data.devices:
                continue
            data.uplinks[serial] = {
                uplink["interface"]: MerakiUplink(
                    interface=uplink["interface"],
                    status=_uplink_status(uplink.get("status")),
                    ip=uplink.get("ip"),
                    gateway=uplink.get("gateway"),
                    public_ip=uplink.get("publicIp"),
                    primary_dns=uplink.get("primaryDns"),
                    secondary_dns=uplink.get("secondaryDns"),
                    ip_assigned_by=uplink.get("ipAssignedBy"),
                )
                for uplink in device.get("uplinks", [])
                if uplink.get("interface")
            }

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
                    self._track_link(switch["serial"], port)
                    port.speed = status.get("speed") or None
                    port.duplex = status.get("duplex") or None
                    port.poe_active = bool((status.get("poe") or {}).get("isAllocated"))

        for switch in topology:
            ports = data.ports.get(switch.get("serial"))
            if not ports:
                continue
            for discovered in switch.get("ports", []):
                if port := ports.get(discovered.get("portId")):
                    port.neighbor = self._current_neighbor(
                        switch["serial"], port, discovered
                    )

        await self._async_add_port_connectors(data)
        await self._async_add_port_energy(data)

    def _track_link(self, serial: str, port: MerakiSwitchPort) -> None:
        """Remember when a port's current link came up."""
        key = (serial, port.port_id)
        previous = self._link_status.get(key)
        if port.status == "Connected" and previous not in (None, "Connected"):
            self._link_up_since[key] = dt_util.utcnow()
        self._link_status[key] = port.status

    def _current_neighbor(
        self, serial: str, port: MerakiSwitchPort, discovered: dict[str, Any]
    ) -> str | None:
        """Return the port's neighbor, unless it was seen before the current link."""
        if port.status != "Connected":
            return None
        # Meraki keeps the last LLDP/CDP data of a port after the device is unplugged
        # and only updates it when a neighbor announces itself again.
        up_since = self._link_up_since.get((serial, port.port_id))
        updated = dt_util.parse_datetime(discovered.get("lastUpdatedAt") or "")
        if up_since is not None and (updated is None or updated < up_since):
            return None
        return _neighbor_name(discovered)

    async def _async_add_port_energy(self, data: MerakiData) -> None:
        """Add PoE energy statistics and the current PoE power of each port."""
        now = dt_util.utcnow()
        fetched_at = self._port_energy_fetched_at
        if fetched_at is None or now - fetched_at >= PORT_ENERGY_INTERVAL:
            await self._async_fetch_port_energy(data, now)
        for (serial, port_id), watts in self._poe_power.items():
            if port := data.ports.get(serial, {}).get(port_id):
                port.poe_power = watts
        data.switch_poe_power = {
            serial: watts
            for serial, watts in self._switch_poe_power.items()
            if serial in data.ports
        }

    async def _async_fetch_port_energy(self, data: MerakiData, now: datetime) -> None:
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
        self._port_energy_fetched_at = now

        for switch in _unwrap_items(usage):
            serial = switch.get("serial")
            ports = data.ports.get(serial)
            if not ports:
                continue
            # Interval start -> total PoE power (W) of the switch's PoE ports.
            totals: dict[datetime, float] = {}
            for usage_port in switch.get("ports", []):
                port = ports.get(usage_port.get("portId"))
                if port is None:
                    continue
                intervals = _energy_slots(
                    sorted(
                        (dt_util.as_utc(ts), wh)
                        for interval in usage_port.get("intervals", [])
                        if (ts := dt_util.parse_datetime(interval.get("startTs") or ""))
                        and (wh := _interval_energy(interval)) is not None
                    )
                )
                if not intervals:
                    continue
                stat = await self._async_port_energy_statistic(data, serial, port)
                # PoE ports that have never delivered power would only add sensors
                # stuck at zero; they are picked up once something draws power.
                if (
                    stat.watermark is None
                    and not stat.hour_sum
                    and not port.poe_active
                    and not any(wh for _, wh in intervals)
                ):
                    continue
                power_stat = await self._async_poe_power_statistic(
                    data, serial, f"port_{port.port_id}_poe_power"
                )
                for ts, wh in intervals:
                    # Rounded to the milliwatt, which is far below Meraki's 1.2 W steps.
                    watts = round(wh * WATT_HOURS_TO_WATTS, 3)
                    stat.add(ts, wh)
                    power_stat.add(ts, watts)
                    totals[ts] = round(totals.get(ts, 0.0) + watts, 3)
                self._poe_power[(serial, port.port_id)] = round(
                    intervals[-1][1] * WATT_HOURS_TO_WATTS, 3
                )
            if totals:
                total_stat = await self._async_poe_power_statistic(
                    data, serial, "poe_power"
                )
                for ts, watts in sorted(totals.items()):
                    total_stat.add(ts, watts)
                self._switch_poe_power[serial] = totals[max(totals)]
        await self.port_energy.async_refresh_and_write()
        await self.poe_power_stats.async_refresh_and_write()

    async def _async_poe_power_statistic(
        self, data: MerakiData, serial: str, key: str
    ) -> MerakiStatistic:
        """Return the PoE power statistic of a port or, for "poe_power", a switch."""
        return await self.poe_power_stats.async_ensure(
            statistic_id(serial, key),
            f"{data.devices[serial].name} {key}",
            UnitOfPower.WATT,
            StatisticKind.MEAN,
            entity_unique_id=f"{serial}_{key}",
        )

    async def _async_port_energy_since(
        self, data: MerakiData, now: datetime
    ) -> datetime:
        """Return the time from which port energy has to be fetched."""
        if not self.port_energy.statistics:
            # First run: continue where the statistics of PoE ports left off. The
            # power statistics resume a little earlier, to rewrite the newest hours.
            for serial, ports in data.ports.items():
                for port in ports.values():
                    if port.poe_enabled:
                        await self._async_port_energy_statistic(data, serial, port)
                        await self._async_poe_power_statistic(
                            data, serial, f"port_{port.port_id}_poe_power"
                        )
            resume = [
                stat.hour_done
                for writer in (self.port_energy, self.poe_power_stats)
                for stat in writer.statistics.values()
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
        # its next config sync, usually within a minute or two; the link state
        # follows on a later poll.
        self.data.ports[serial][port_id].enabled = enabled
        self.async_update_listeners()

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
            # Clients seen earlier in the look-back window may have been unplugged.
            if client.get("status") != "Online":
                continue
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


def _energy_slots(
    intervals: list[tuple[datetime, float]],
) -> list[tuple[datetime, float]]:
    """
    Return the energy (Wh) per 5-minute slot of a port's usage intervals.

    Meraki sometimes leaves out an interval and reports its energy with the one
    before, still labelled as 5 minutes long. Taken at face value, that interval
    would show double the power and the missing one none, so the energy of an
    interval followed by a short gap is spread over the slots up to the next one.
    """
    slots: list[tuple[datetime, float]] = []
    if not intervals:
        return slots
    next_starts: list[datetime | None] = [start for start, _ in intervals[1:]]
    for (start, wh), next_start in zip(intervals, [*next_starts, None], strict=True):
        count = 1
        if next_start is not None and next_start - start <= MAX_MERGED_INTERVAL:
            count = max(round((next_start - start) / SHORT_TERM), 1)
        slots.extend((start + SHORT_TERM * slot, wh / count) for slot in range(count))
    return slots


def _interval_energy(interval: dict[str, Any]) -> float | None:
    """Return the energy (Wh) of a port usage interval, if it has any."""
    usage = (interval.get("energy") or {}).get("usage") or {}
    total = usage.get("total")
    return float(total) if isinstance(total, int | float) else None


def _uplink_status(status: str | None) -> str | None:
    """Return an uplink status as one of UPLINK_STATUSES, or None if unknown."""
    if status is None:
        return None
    # The API reports "not connected" with a space.
    normalized = status.lower().replace(" ", "_")
    if normalized not in UPLINK_STATUSES:
        LOGGER.debug("Unknown uplink status %s", status)
        return None
    return normalized

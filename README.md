<img src="https://raw.githubusercontent.com/felixblang/ha-cisco-meraki/main/custom_components/cisco_meraki/brand/logo.png" alt="Cisco Meraki" height="64">

# Cisco Meraki for Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories)

A Home Assistant custom integration for [Cisco Meraki](https://meraki.cisco.com/)
organizations: device status and clients, switch ports you can turn on and off with
their PoE power and energy, and MT sensors whose readings are recorded at the time
they were measured, from the Meraki Dashboard API or locally over MQTT.

> [!NOTE]
> This is unrelated to the built-in [Meraki](https://www.home-assistant.io/integrations/meraki/)
> integration, which receives client locations through the Meraki CMX API.

## Features

Every device in the organization (or in the networks you select) shows up as a
Home Assistant device with:

| Entity | Description |
| -- | -- |
| **Status** | `online`, `offline`, `alerting` or `dormant`, as reported by the Dashboard. |
| **Clients** | Number of clients currently online on the device. Access points, switches and security appliances only. |

Security appliances (MX, Z) and cellular gateways (MG) get a status sensor per WAN
uplink, named **WAN 1**, **WAN 2** or **Cellular**: `active` (carrying traffic),
`ready` (standing by), `connecting`, `not_connected` or `failed`. Attributes show the
uplink's IP, gateway, public IP, DNS servers and whether the address is static or from
DHCP.

Each uplink also gets **latency** (ms) and **packet loss** (%) sensors. The appliance
measures both every minute against a probe destination (shown in the `destination`
attribute; IPv4 is preferred when it probes several). Like the MT sensors, the
measurements arrive a few minutes late and are written as statistics at the minute
they were measured, so a degrading line shows up in the graphs exactly when it
happened.

For example, to be notified when the primary uplink stops carrying traffic:

```yaml
automation:
  - alias: "WAN 1 is down"
    triggers:
      - trigger: state
        entity_id: sensor.mx67_1_wan_1
        from: active
        for: "00:02:00"
    actions:
      - action: notify.notify
        data:
          message: "WAN 1 is {{ states('sensor.mx67_1_wan_1') }}"
```

Switches also get two entities per port:

| Entity | Description |
| -- | -- |
| **Port _n_** | Switch that enables or disables the port in the Dashboard. Attributes show the port description, type and VLAN. |
| **Port _n_ link** | Connectivity sensor that is on while something is connected. Attributes show the connector (`RJ45` or `SFP`), speed and duplex. |

Ports that deliver PoE also get **Port _n_ PoE power** (average W over the newest
5-minute interval) and **Port _n_ PoE energy** (kWh), and the link sensor's `poe`
attribute shows whether PoE is active. The PoE sensors appear as soon as a port starts
delivering PoE; their values follow once Meraki reports the first 5-minute interval,
usually within 5 to 10 minutes. Each switch also gets a **PoE power** sensor with the
total of all its ports.

Meraki reports PoE energy in steps of 0.1 Wh per 5-minute interval, so PoE power moves
in steps of 1.2 W. Like the MT sensors, the PoE power sensors write their statistics
at the time of each interval, so their graphs show the 5-minute values and, for longer
periods, the hourly mean, minimum and maximum. That covers devices that only draw power and
have no network link, such as Meraki MT sensors. Like the MT40's energy, PoE energy is
written as statistics for the 5-minute intervals in which it was used; see the
`statistic_id` attribute.

Both port entities also show what is connected, so you can see what you are about to
switch off:

- `neighbor`: the LLDP system name (or CDP device ID) of the device on the other end,
  such as an access point or another switch.
- `clients`: wired clients currently online on the port, by their Dashboard name.

Meraki keeps a port's LLDP/CDP data after the device is unplugged and only updates it
when a new neighbor announces itself. The integration therefore hides the neighbor
while a port has no link, and after the link comes back until Meraki reports newer
data, so a device that does not speak LLDP leaves the field empty instead of showing
the previous one.

> [!WARNING]
> Turning off a port disconnects whatever is plugged into it, which may be the device
> running Home Assistant. Switches for uplink and stacking ports are created disabled;
> enable them in the entity settings only if you really want to control them.

Device, port and uplink status are polled every minute, so new devices and ports,
and changes made in the Dashboard, show up within a minute or so. PoE energy and MT
sensor readings are polled every 5 minutes, since they only change in 5-minute
intervals or arrive in 20-minute batches.

When you turn a port on or off in Home Assistant, the switch shows the new setting
right away. The Meraki switch itself applies the change on its next sync with the
Dashboard, usually within a minute or two, and the link sensor follows on a later poll.

### MT sensors

MT sensors get a sensor per metric they report: temperature, humidity, CO2, PM2.5,
TVOC, noise and indoor air quality (MT15), battery, and power, apparent power,
voltage, current, frequency and power factor (MT40). The MT40 also gets an **Energy**
sensor, integrated from its power readings.

Meraki sensors upload their readings in batches, often 10 to 20 minutes after they
were measured. Home Assistant records a state at the moment it is written, so graphs
built from the states would show each batch bunched into a single moment. This
integration therefore keeps the two apart:

- The **sensor state** is the latest reading, written when its batch arrives. The
  `measured_at` attribute holds the time it was actually measured.
- **Statistics** are written for the time each reading was measured: 5-minute
  statistics (kept 10 days) and hourly statistics (kept forever), each with mean,
  minimum and maximum. They go into the sensor's own statistics, so the graph in the
  sensor's dialog and any Statistics graph card show them like any other sensor's.

Home Assistant itself also compiles each sensor's 5-minute statistics from its
states, right after each period. The integration replaces those once the batch with
the real readings has arrived, so the newest 20 minutes or so of a graph look stepped
at first and are corrected with the next batch.

The MT40's **Energy** works differently, because a running sum cannot be corrected
after Home Assistant has added to it. Its correctly timed statistics have their own
ID, shown in the sensor's `statistic_id` attribute (e.g.
`cisco_meraki:q3cj_xxxx_xxxx_energy`). Add that statistic in **Settings > Dashboards >
Energy**; the energy dashboard then shows it per hour, day, week and month. The same
applies to PoE energy of switch ports.

#### Readings over MQTT

If your Meraki network publishes sensor readings to an MQTT broker (set up in the
network's MQTT broker settings in the Dashboard) and Home Assistant's
[MQTT integration](https://www.home-assistant.io/integrations/mqtt/) is connected to
the same broker, the integration takes the readings from there. Sensors that publish
over MQTT are no longer polled through the API. A sensor that has not published for 30
minutes is polled through the API again, and the API also fills the gap after a
restart. Readings that arrive through both are only counted once.

MQTT batches are no faster than the API: sensors publish their readings every 20
minutes or so. Some sensors, such as an MT40 the Dashboard reports as dormant, may only
deliver their readings over MQTT.

Statistics are only written for periods Home Assistant has already compiled itself,
so they never get in the way of its own statistics, also not while it catches up after
a restart. After a restart the integration fetches readings again from the last
complete hour, up to 24 hours back.

> [!NOTE]
> Home Assistant only offers a public way to write hourly statistics. The 5-minute
> statistics are written through the recorder's internal import, which a future Home
> Assistant release may change; the integration then falls back to hourly statistics
> and logs a warning.

### MV cameras (MV Sense)

If your MV cameras publish MV Sense data to an MQTT broker (set up in the camera's
Sense settings in the Dashboard) and Home Assistant's MQTT integration is connected to
the same broker, each camera gets:

- **_Zone_ people** for each of its analytics zones (the full frame and every zone you
  drew in the Dashboard, by their Dashboard names), and **_Zone_ vehicles** on cameras
  that count vehicles.
- **Illuminance** (lx), on cameras with a light sensor.
- **Audio level** (dBFS), on cameras with a microphone. It is relative to the
  microphone's full scale, not a sound pressure, so it starts disabled.

The sensors appear as soon as a camera publishes the value. Cameras publish within a
few seconds and repeat their counts several times a second; only changes are written,
and Home Assistant compiles the statistics as for any other sensor.

Cameras that have not published over MQTT for 2 minutes, or whose broker Home
Assistant cannot reach, are polled through the Dashboard API every minute instead.
The API only provides people counts: each zone's average over the last minute, rounded
to whole people. Vehicle counts, illuminance and audio level need MQTT.

## Trademarks

This is an independent project and is not affiliated with or endorsed by Cisco. Cisco
Meraki and the Cisco Meraki logo are trademarks of Cisco Systems, Inc.; the logo is
the one Home Assistant uses for its built-in Meraki integration.

## Installation

### HACS (custom repository)

1. In HACS, open the menu in the top right and choose **Custom repositories**.
2. Add `https://github.com/felixblang/ha-cisco-meraki` with the type **Integration**.
3. Install **Cisco Meraki** and restart Home Assistant.

### Manual

Copy `custom_components/cisco_meraki` into the `custom_components` folder of your
Home Assistant configuration and restart Home Assistant.

## Configuration

1. In the Meraki Dashboard, enable API access and create an API key under
   **Organization > API & Webhooks**. A read-only administrator's key is enough for
   monitoring; turning switch ports on and off needs a key with full access.
2. In Home Assistant, go to **Settings > Devices & services > Add integration** and
   search for **Cisco Meraki**.
3. Enter the API key. If it has access to several organizations, pick one. Repeat the
   setup to add more organizations.
4. Choose which networks to include. Leave the selection empty to include every
   network, including ones added later.

To change the networks later, open the integration's **Configure** dialog.

If the API key is revoked, Home Assistant asks you to reauthenticate with a new one.

## API usage

Every minute, the integration makes two organization-wide requests for devices and
their status, and one client request per network that has an access point, switch or
appliance. If there are appliances or gateways, it adds one request for uplink status
and one or two for loss and latency (the API only returns 5-minute windows; after a
restart up to an hour is fetched again). If there are switches, it adds one request per
50 switches for port configuration and one per 20 switches each for port status and
LLDP/CDP neighbors.

Every 5 minutes, it also requests PoE energy (one request per 50 switches) and, if the
organization has MT sensors, their readings history; fast metrics such as PM2.5 report
every second, so this is usually a few pages. Each switch's port connectors are looked
up once, the first time the switch is seen.

For a small organization that is around 10 requests a minute. The Meraki API allows
10 requests per second per organization; the integration retries automatically when it
is rate limited.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements_test.txt
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

`scripts/develop` starts a local Home Assistant instance with the integration loaded,
using the `config` directory. A VS Code dev container is included.

Outside the dev container, Home Assistant needs a C++ compiler and a few system
libraries. Without them the voice assistant packages fail to build, which breaks
`default_config` and parts of the frontend. On Debian or Ubuntu:

```bash
sudo apt install build-essential ffmpeg libturbojpeg libpcap-dev
```

## License

[MIT](LICENSE)

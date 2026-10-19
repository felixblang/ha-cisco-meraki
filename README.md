# Meraki Dashboard for Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories)

A Home Assistant custom integration that monitors the devices of a
[Cisco Meraki](https://meraki.cisco.com/) organization through the Meraki Dashboard API.

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

New devices are picked up on the next poll. Data refreshes every 5 minutes.

## Installation

### HACS (custom repository)

1. In HACS, open the menu in the top right and choose **Custom repositories**.
2. Add `https://github.com/felixblang/hass_meraki_intergration` with the type **Integration**.
3. Install **Meraki Dashboard** and restart Home Assistant.

### Manual

Copy `custom_components/hass_meraki` into the `custom_components` folder of your
Home Assistant configuration and restart Home Assistant.

## Configuration

1. In the Meraki Dashboard, enable API access and create an API key under
   **Organization > API & Webhooks**. A key for a read-only administrator is enough.
2. In Home Assistant, go to **Settings > Devices & services > Add integration** and
   search for **Meraki Dashboard**.
3. Enter the API key. If it has access to several organizations, pick one. Repeat the
   setup to add more organizations.

To limit the integration to some networks, open the integration's **Configure** dialog
and select them. Leave the selection empty to include every network.

If the API key is revoked, Home Assistant asks you to reauthenticate with a new one.

## API usage

Each poll makes two organization-wide requests plus one client request per network
that has an access point, switch or appliance. The Meraki API allows 10 requests per
second per organization; the integration retries automatically when it is rate limited.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements_test.txt
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

`scripts/develop` starts a local Home Assistant instance with the integration loaded,
using the `config` directory. A VS Code dev container is included.

## License

[MIT](LICENSE)

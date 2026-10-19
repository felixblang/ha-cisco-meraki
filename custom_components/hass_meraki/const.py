"""Constants for the Meraki Dashboard integration."""

from datetime import timedelta
from logging import Logger, getLogger

LOGGER: Logger = getLogger(__package__)

DOMAIN = "hass_meraki"
MANUFACTURER = "Cisco Meraki"

CONF_ORGANIZATION_ID = "organization_id"
CONF_NETWORKS = "networks"

DEFAULT_SCAN_INTERVAL = timedelta(minutes=5)

# Look-back window used when counting online clients per device.
CLIENT_TIMESPAN_SECONDS = 3600

# Product types that serve clients and therefore get a client count sensor.
CLIENT_PRODUCT_TYPES = frozenset({"appliance", "switch", "wireless"})

DEVICE_STATUSES = ["online", "offline", "alerting", "dormant"]

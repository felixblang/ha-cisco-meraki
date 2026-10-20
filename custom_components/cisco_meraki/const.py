"""Constants for the Cisco Meraki integration."""

from datetime import timedelta
from logging import Logger, getLogger

LOGGER: Logger = getLogger(__package__)

DOMAIN = "cisco_meraki"
MANUFACTURER = "Cisco Meraki"

CONF_ORGANIZATION_ID = "organization_id"
CONF_NETWORKS = "networks"

DEFAULT_SCAN_INTERVAL = timedelta(minutes=5)

# Seconds after a port change at which to poll again, to pick up the new link state
# once the switch has applied the change.
PORT_CHANGE_REFRESH_DELAYS = (45, 90)

# Port energy usage is fetched for at least this span (shorter ones return nothing)
# and at most this far back after a restart.
PORT_USAGE_MIN_SPAN = timedelta(hours=1)
PORT_USAGE_MAX_BACKFILL = timedelta(hours=24)

# Uplink loss and latency can only be requested in windows of at most 5 minutes that
# end at least 2 minutes ago; after a restart, at most this much is fetched again.
UPLINK_QUALITY_WINDOW = timedelta(minutes=5)
UPLINK_QUALITY_DELAY = timedelta(minutes=2)
UPLINK_QUALITY_MAX_BACKFILL = timedelta(hours=1)

# Look-back window used when counting online clients per device.
CLIENT_TIMESPAN_SECONDS = 3600

# Product types that serve clients and therefore get a client count sensor.
CLIENT_PRODUCT_TYPES = frozenset({"appliance", "switch", "wireless"})

DEVICE_STATUSES = ["online", "offline", "alerting", "dormant"]

# Devices with WAN uplinks: security appliances, teleworker gateways and cellular
# gateways.
UPLINK_PRODUCT_TYPES = frozenset({"appliance", "cellularGateway"})
UPLINK_STATUSES = ["active", "ready", "connecting", "not_connected", "failed"]

"""Helpers around the Meraki Dashboard API client."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from meraki.aio import AsyncDashboardAPI

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from meraki import APIError

# Meraki asks API callers to identify as "<Application>/<version> <vendor>".
CALLER = "HomeAssistantMeraki/0.5.0 felixblang"


async def async_create_client(hass: HomeAssistant, api_key: str) -> AsyncDashboardAPI:
    """Create a dashboard client without blocking the event loop."""
    # The underlying httpx client loads SSL certificates on construction, which is
    # blocking I/O, so the client is built in the executor.
    return await hass.async_add_executor_job(
        partial(
            AsyncDashboardAPI,
            api_key=api_key,
            caller=CALLER,
            suppress_logging=True,
            # Smart flow persists a cache file under ~/.meraki; HA polls slowly enough
            # that the SDK's reactive 429 handling is sufficient.
            smart_flow_enabled=False,
        )
    )


async def async_close_client(client: AsyncDashboardAPI) -> None:
    """Close the client's HTTP connections."""
    await client.__aexit__(None, None, None)


def is_auth_error(err: APIError) -> bool:
    """Return True if the error means the API key is invalid or lacks access."""
    return err.status in (401, 403)

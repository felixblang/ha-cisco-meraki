"""Tests for the Cisco Meraki config flow."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.cisco_meraki.api import CALLER
from custom_components.cisco_meraki.const import (
    CONF_NETWORKS,
    CONF_ORGANIZATION_ID,
    DOMAIN,
)

from .conftest import API_KEY, NETWORK_ID, ORG_ID, ORG_NAME, make_api_error


async def test_single_organization(hass: HomeAssistant, mock_client: MagicMock) -> None:
    """With one organization the flow goes straight to picking networks."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: API_KEY}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "networks"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_NETWORKS: [NETWORK_ID]}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == ORG_NAME
    assert result["data"] == {CONF_API_KEY: API_KEY, CONF_ORGANIZATION_ID: ORG_ID}
    assert result["options"] == {CONF_NETWORKS: [NETWORK_ID]}
    assert result["result"].unique_id == ORG_ID
    mock_client.__aexit__.assert_awaited()


async def test_multiple_organizations(
    hass: HomeAssistant, mock_client: MagicMock
) -> None:
    """An API key with several organizations asks which one to add."""
    mock_client.organizations.getOrganizations.return_value = [
        {"id": ORG_ID, "name": ORG_NAME},
        {"id": "999", "name": "Other"},
    ]
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: API_KEY}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "organization"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ORGANIZATION_ID: "999"}
    )
    assert result["step_id"] == "networks"

    # An empty selection means all networks.
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"] == {CONF_NETWORKS: []}
    assert result["title"] == "Other"
    assert result["data"][CONF_ORGANIZATION_ID] == "999"


@pytest.mark.parametrize(
    ("side_effect", "error"),
    [
        (make_api_error(401), "invalid_auth"),
        (make_api_error(503), "cannot_connect"),
        (RuntimeError, "unknown"),
    ],
)
async def test_user_errors(
    hass: HomeAssistant,
    mock_client: MagicMock,
    side_effect: Exception | type[Exception],
    error: str,
) -> None:
    """Errors are shown and the flow can recover."""
    mock_client.organizations.getOrganizations.side_effect = side_effect
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: API_KEY}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}

    mock_client.organizations.getOrganizations.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: API_KEY}
    )
    assert result["step_id"] == "networks"


async def test_no_organizations(hass: HomeAssistant, mock_client: MagicMock) -> None:
    """A key without organizations is rejected."""
    mock_client.organizations.getOrganizations.return_value = []
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: API_KEY}
    )
    assert result["errors"] == {"base": "no_organizations"}


async def test_single_network_skips_selection(
    hass: HomeAssistant, mock_client: MagicMock
) -> None:
    """With only one network there is nothing to choose."""
    mock_client.organizations.getOrganizationNetworks.return_value = [
        {"id": NETWORK_ID, "name": "Home"}
    ]
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: API_KEY}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"] == {CONF_NETWORKS: []}


async def test_already_configured(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """The same organization cannot be added twice."""
    mock_config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: API_KEY}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """Reauth replaces the API key, but only with one that can see the org."""
    mock_config_entry.add_to_hass(hass)
    result = await mock_config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    mock_client.organizations.getOrganizations.return_value = [
        {"id": "999", "name": "Other"}
    ]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: "wrong-org-key"}
    )
    assert result["errors"] == {"base": "organization_not_accessible"}

    mock_client.organizations.getOrganizations.return_value = [
        {"id": ORG_ID, "name": ORG_NAME}
    ]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: "new-key"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data[CONF_API_KEY] == "new-key"
    # Reauth reloads the entry; let that finish before the test ends.
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_options_flow(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    """Networks can be selected in the options and the entry reloads."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_NETWORKS: ["N_2"]}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert mock_config_entry.options == {CONF_NETWORKS: ["N_2"]}

    # Both test devices are in NETWORK_ID, so the filter leaves nothing.
    assert NETWORK_ID not in mock_config_entry.options[CONF_NETWORKS]
    assert hass.states.get("sensor.office_ap_status").state == "unavailable"


def test_caller_version_matches_manifest() -> None:
    """The version reported to Meraki is the integration's version."""
    manifest = json.loads(
        (
            Path(__file__).parent.parent
            / "custom_components/cisco_meraki/manifest.json"
        ).read_text()
    )
    assert CALLER.split()[0].split("/")[1] == manifest["version"]

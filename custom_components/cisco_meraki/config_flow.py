"""Config flow for the Cisco Meraki integration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_API_KEY
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from meraki import APIError

from .api import async_close_client, async_create_client, is_auth_error
from .const import CONF_NETWORKS, CONF_ORGANIZATION_ID, DOMAIN, LOGGER

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .coordinator import MerakiConfigEntry

API_KEY_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_API_KEY): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        )
    }
)


def networks_schema(networks: list[dict[str, Any]]) -> vol.Schema:
    """Return a schema to pick networks; an empty selection means all of them."""
    options = [
        SelectOptionDict(
            value=network["id"], label=network.get("name") or network["id"]
        )
        for network in sorted(networks, key=lambda n: (n.get("name") or "").lower())
    ]
    return vol.Schema(
        {
            vol.Optional(CONF_NETWORKS): SelectSelector(
                SelectSelectorConfig(
                    options=options, multiple=True, mode=SelectSelectorMode.DROPDOWN
                )
            )
        }
    )


class MerakiConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Cisco Meraki."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._api_key: str = ""
        self._organizations: dict[str, str] = {}
        self._organization_id: str = ""
        self._networks: list[dict[str, Any]] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the API key and look up the organizations it can access."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._api_key = user_input[CONF_API_KEY]
            organizations, error = await self._async_fetch_organizations(self._api_key)
            if error:
                errors["base"] = error
            elif not organizations:
                errors["base"] = "no_organizations"
            else:
                self._organizations = organizations
                if len(organizations) == 1:
                    return await self._async_select_organization(
                        next(iter(organizations))
                    )
                return await self.async_step_organization()

        return self.async_show_form(
            step_id="user", data_schema=API_KEY_SCHEMA, errors=errors
        )

    async def async_step_organization(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user pick one of the accessible organizations."""
        if user_input is not None:
            return await self._async_select_organization(
                user_input[CONF_ORGANIZATION_ID]
            )

        options = [
            SelectOptionDict(value=org_id, label=name)
            for org_id, name in sorted(
                self._organizations.items(), key=lambda item: item[1].lower()
            )
        ]
        return self.async_show_form(
            step_id="organization",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ORGANIZATION_ID): SelectSelector(
                        SelectSelectorConfig(
                            options=options, mode=SelectSelectorMode.DROPDOWN
                        )
                    )
                }
            ),
        )

    async def async_step_networks(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user pick which networks of the organization to include."""
        if user_input is not None:
            return self._async_create_org_entry(user_input.get(CONF_NETWORKS, []))

        return self.async_show_form(
            step_id="networks",
            data_schema=networks_schema(self._networks),
            description_placeholders={
                "organization": self._organizations[self._organization_id]
            },
        )

    async def async_step_reauth(
        self,
        entry_data: Mapping[str, Any],  # noqa: ARG002
    ) -> ConfigFlowResult:
        """Start reauthentication after the API key stopped working."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a new API key."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()

        if user_input is not None:
            organizations, error = await self._async_fetch_organizations(
                user_input[CONF_API_KEY]
            )
            if error:
                errors["base"] = error
            elif entry.unique_id not in organizations:
                errors["base"] = "organization_not_accessible"
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_API_KEY: user_input[CONF_API_KEY]}
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=API_KEY_SCHEMA,
            description_placeholders={"organization": entry.title},
            errors=errors,
        )

    async def _async_select_organization(
        self, organization_id: str
    ) -> ConfigFlowResult:
        await self.async_set_unique_id(organization_id)
        self._abort_if_unique_id_configured()
        self._organization_id = organization_id

        client = await async_create_client(self.hass, self._api_key)
        try:
            self._networks = await client.organizations.getOrganizationNetworks(
                organization_id, total_pages="all"
            )
        except APIError as err:
            LOGGER.debug("Could not list networks: %s", err)
            return self.async_abort(reason="cannot_connect")
        finally:
            await async_close_client(client)

        if len(self._networks) <= 1:
            return self._async_create_org_entry([])
        return await self.async_step_networks()

    @callback
    def _async_create_org_entry(self, networks: list[str]) -> ConfigFlowResult:
        return self.async_create_entry(
            title=self._organizations[self._organization_id],
            data={
                CONF_API_KEY: self._api_key,
                CONF_ORGANIZATION_ID: self._organization_id,
            },
            options={CONF_NETWORKS: networks},
        )

    async def _async_fetch_organizations(
        self, api_key: str
    ) -> tuple[dict[str, str], str | None]:
        """Return ({org_id: name}, error_key) for the given API key."""
        client = await async_create_client(self.hass, api_key)
        try:
            organizations = await client.organizations.getOrganizations(
                total_pages="all"
            )
        except APIError as err:
            if is_auth_error(err):
                return {}, "invalid_auth"
            LOGGER.debug("Could not connect to the Meraki Dashboard API: %s", err)
            return {}, "cannot_connect"
        except Exception:  # noqa: BLE001
            LOGGER.exception("Unexpected error while validating the API key")
            return {}, "unknown"
        finally:
            await async_close_client(client)
        return {org["id"]: org.get("name") or org["id"] for org in organizations}, None

    @staticmethod
    def async_get_options_flow(
        config_entry: MerakiConfigEntry,  # noqa: ARG004
    ) -> MerakiOptionsFlow:
        """Return the options flow handler."""
        return MerakiOptionsFlow()


class MerakiOptionsFlow(OptionsFlow):
    """Let the user restrict the integration to a subset of networks."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose which networks to include."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        entry: MerakiConfigEntry = self.config_entry
        if entry.state is not ConfigEntryState.LOADED:
            return self.async_abort(reason="not_loaded")
        try:
            networks = (
                await entry.runtime_data.client.organizations.getOrganizationNetworks(
                    entry.data[CONF_ORGANIZATION_ID], total_pages="all"
                )
            )
        except APIError as err:
            LOGGER.debug("Could not list networks: %s", err)
            return self.async_abort(reason="cannot_connect")

        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                networks_schema(networks),
                entry.options,
            ),
        )

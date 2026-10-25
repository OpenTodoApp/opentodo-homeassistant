"""Config flow: the server address and an API token; options for the lists and updates."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlowWithReload
from homeassistant.const import CONF_TOKEN, CONF_URL
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
import voluptuous as vol
from yarl import URL

from .api import OpenTodoApiError, OpenTodoAuthError, OpenTodoClient, OpenTodoConnectionError, OpenTodoNotOpenTodoError
from .const import (
    CONF_ALL_PROJECTS,
    CONF_COMPLETED_DAYS,
    CONF_HA_URL,
    CONF_PROJECTS,
    CONF_PUSH,
    CONF_QUICK_ADD,
    CONF_SCAN_INTERVAL,
    CONF_TODAY_LIST,
    CONF_VERIFY_SSL,
    DEFAULT_COMPLETED_DAYS,
    DEFAULT_SCAN_INTERVAL,
    DOCS_URL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
    SCOPE_ADMIN,
    SCOPE_AGENT,
)

_LOGGER = logging.getLogger(__name__)

TOKEN_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
URL_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.URL))


class CannotConnect(Exception):
    """The server cannot be reached."""


class NotOpenTodo(Exception):
    """The address does not answer like OpenTodo."""


class InvalidAuth(Exception):
    """The token was refused."""


class FlowError(Exception):
    """A refusal with its own message key."""

    def __init__(self, key: str) -> None:
        """Keep the error key for the form."""
        super().__init__(key)
        self.key = key


async def validate(hass: HomeAssistant, url: str, token: str, verify_ssl: bool) -> dict[str, Any]:
    """Check the server and the token; return what the entry needs."""
    client = OpenTodoClient(
        async_get_clientsession(hass, verify_ssl=verify_ssl), url, token.strip(), verify_ssl=verify_ssl
    )
    try:
        health = await client.health()
        me = await client.me()
    except OpenTodoNotOpenTodoError as err:
        raise FlowError("not_opentodo") from err
    except OpenTodoConnectionError as err:
        raise FlowError("cannot_connect") from err
    except OpenTodoAuthError as err:
        raise FlowError("invalid_auth") from err
    except OpenTodoApiError as err:
        _LOGGER.debug("OpenTodo refused the check: %s", err)
        raise FlowError("unknown") from err
    scope = str((me.get("auth") or {}).get("scope", "full"))
    if scope == SCOPE_AGENT:
        raise FlowError("agent_token")
    if scope == SCOPE_ADMIN:
        raise FlowError("admin_token")
    if (me.get("encryption") or {}).get("enabled"):
        raise FlowError("encrypted_account")
    user = me.get("user") or {}
    if not user.get("id"):
        raise FlowError("unknown")
    host = URL(client.url).host or client.url
    name = user.get("name") or user.get("email") or "OpenTodo"
    return {
        "url": client.url,
        "user_id": user["id"],
        "title": f"{name} ({host})",
        "scope": scope,
        "version": health.get("version"),
    }


def _user_schema(defaults: Mapping[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_URL, default=defaults.get(CONF_URL, "")): URL_SELECTOR,
            vol.Required(CONF_TOKEN): TOKEN_SELECTOR,
            vol.Required(CONF_VERIFY_SSL, default=defaults.get(CONF_VERIFY_SSL, True)): BooleanSelector(),
        }
    )


PLACEHOLDERS = {
    "docs_url": DOCS_URL,
    "url_example": "http://192.168.1.20:8080, https://todo.example.com",
    "ha_url_example": "http://192.168.1.10:8123",
}


class OpenTodoConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up one OpenTodo account."""

    VERSION = 1
    MINOR_VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the server and a token."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                info = await validate(
                    self.hass, user_input[CONF_URL], user_input[CONF_TOKEN], user_input[CONF_VERIFY_SSL]
                )
            except FlowError as err:
                errors["base"] = err.key
            else:
                await self.async_set_unique_id(info["user_id"])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=info["title"],
                    data={
                        CONF_URL: info["url"],
                        CONF_TOKEN: user_input[CONF_TOKEN].strip(),
                        CONF_VERIFY_SSL: user_input[CONF_VERIFY_SSL],
                    },
                )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(_user_schema(user_input or {}), user_input or {}),
            errors=errors,
            description_placeholders=PLACEHOLDERS,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """The token stopped working."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for a new token for the same account."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                info = await validate(
                    self.hass, entry.data[CONF_URL], user_input[CONF_TOKEN], entry.data.get(CONF_VERIFY_SSL, True)
                )
            except FlowError as err:
                errors["base"] = err.key
            else:
                await self.async_set_unique_id(info["user_id"])
                self._abort_if_unique_id_mismatch(reason="wrong_account")
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_TOKEN: user_input[CONF_TOKEN].strip()}
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_TOKEN): TOKEN_SELECTOR}),
            errors=errors,
            description_placeholders={**PLACEHOLDERS, "url": entry.data[CONF_URL], "title": entry.title},
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Change the address or the token of the same account."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                info = await validate(
                    self.hass, user_input[CONF_URL], user_input[CONF_TOKEN], user_input[CONF_VERIFY_SSL]
                )
            except FlowError as err:
                errors["base"] = err.key
            else:
                await self.async_set_unique_id(info["user_id"])
                self._abort_if_unique_id_mismatch(reason="wrong_account")
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_URL: info["url"],
                        CONF_TOKEN: user_input[CONF_TOKEN].strip(),
                        CONF_VERIFY_SSL: user_input[CONF_VERIFY_SSL],
                    },
                )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_user_schema(user_input or entry.data),
            errors=errors,
            description_placeholders=PLACEHOLDERS,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: Any) -> OpenTodoOptionsFlow:
        """The options: lists, completed tasks, updates and quick add."""
        return OpenTodoOptionsFlow()


class OpenTodoOptionsFlow(OptionsFlowWithReload):
    """Choose the lists and how updates arrive."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """One form with every option."""
        if user_input is not None:
            if not user_input.get(CONF_HA_URL):
                user_input.pop(CONF_HA_URL, None)
            return self.async_create_entry(data=user_input)
        options = self.config_entry.options
        coordinator = getattr(self.config_entry, "runtime_data", None)
        projects = coordinator.data.active_projects() if coordinator is not None and coordinator.data else []
        choices = [SelectOptionDict(value=p["id"], label=str(p.get("name", p["id"]))) for p in projects]
        schema = vol.Schema(
            {
                vol.Required(CONF_ALL_PROJECTS, default=options.get(CONF_ALL_PROJECTS, True)): BooleanSelector(),
                vol.Optional(CONF_PROJECTS, default=options.get(CONF_PROJECTS, [])): SelectSelector(
                    SelectSelectorConfig(options=choices, multiple=True, mode=SelectSelectorMode.LIST)
                ),
                vol.Required(CONF_TODAY_LIST, default=options.get(CONF_TODAY_LIST, True)): BooleanSelector(),
                vol.Required(
                    CONF_COMPLETED_DAYS, default=options.get(CONF_COMPLETED_DAYS, DEFAULT_COMPLETED_DAYS)
                ): NumberSelector(NumberSelectorConfig(min=0, max=30, step=1, mode=NumberSelectorMode.BOX)),
                vol.Required(CONF_QUICK_ADD, default=options.get(CONF_QUICK_ADD, True)): BooleanSelector(),
                vol.Required(
                    CONF_SCAN_INTERVAL, default=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL, step=1, mode=NumberSelectorMode.BOX
                    )
                ),
                vol.Required(CONF_PUSH, default=options.get(CONF_PUSH, True)): BooleanSelector(),
                vol.Optional(CONF_HA_URL, description={"suggested_value": options.get(CONF_HA_URL)}): URL_SELECTOR,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, description_placeholders=PLACEHOLDERS)

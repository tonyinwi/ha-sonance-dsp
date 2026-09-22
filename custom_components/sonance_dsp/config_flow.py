"""Config flow for the Sonance DSP integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

from .const import (
    CONF_INPUT_LINKS,
    CONF_MAX_DB,
    CONF_SCAN_INTERVAL,
    DEFAULT_HTTP_PORT,
    DEFAULT_MAX_DB,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TCP_PORT,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MAX_VOLUME_DB,
    MIN_SCAN_INTERVAL,
    MIN_VOLUME_DB,
    SOURCE_COUNT,
    input_link_key,
)
from .http_api import AmplifierIdentity, SonanceHttpApi, SonanceHttpError
from .protocol import SonanceConnectionError, SonanceProtocol

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Optional(CONF_PORT, default=DEFAULT_TCP_PORT): int,
    }
)

def _options_schema() -> vol.Schema:
    """Volume/polling options, plus one upstream link per source.

    The links are what let a zone show what is playing. The amplifier only
    knows it is amplifying line input 2; the player feeding that input is the
    only thing that knows the track.
    """
    fields: dict[Any, Any] = {
        vol.Optional(CONF_MAX_DB, default=DEFAULT_MAX_DB): NumberSelector(
            NumberSelectorConfig(
                min=MIN_VOLUME_DB + 1,
                max=MAX_VOLUME_DB,
                step=1,
                unit_of_measurement="dB",
                mode=NumberSelectorMode.SLIDER,
            )
        ),
        vol.Optional(
            CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL
        ): NumberSelector(
            NumberSelectorConfig(
                min=MIN_SCAN_INTERVAL,
                max=MAX_SCAN_INTERVAL,
                step=1,
                unit_of_measurement="s",
                mode=NumberSelectorMode.BOX,
            )
        ),
    }
    for source in range(1, SOURCE_COUNT + 1):
        fields[vol.Optional(input_link_key(source))] = EntitySelector(
            EntitySelectorConfig(domain="media_player")
        )
    return vol.Schema(fields)


async def async_read_identity(
    hass: HomeAssistant, host: str
) -> AmplifierIdentity:
    """Read the amplifier's identity over HTTP."""
    api = SonanceHttpApi(async_get_clientsession(hass), host, DEFAULT_HTTP_PORT)
    return await api.identity()


async def async_test_control_connection(host: str, port: int) -> None:
    """Prove the TCP control port accepts us, then let go of it immediately.

    Checked separately from identity, and only after the duplicate check, for
    one reason: the amplifier accepts a single control session. Opening one
    while an existing config entry holds it would knock that entry offline just
    to validate a duplicate we are about to reject.
    """
    client = SonanceProtocol(host, port)
    try:
        await client.connect()
    finally:
        await client.disconnect()


class SonanceConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Sonance DSP."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host: str = user_input[CONF_HOST]
            port: int = user_input.get(CONF_PORT, DEFAULT_TCP_PORT)
            try:
                identity = await async_read_identity(self.hass, host)
            except SonanceHttpError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error reading amplifier identity")
                errors["base"] = "unknown"
            else:
                # Serial, never the IP: these amplifiers are commonly on DHCP
                # and an address change must not orphan the entry. Done before
                # the TCP test so a duplicate never disturbs the live entry's
                # control session.
                await self.async_set_unique_id(identity.serial)
                self._abort_if_unique_id_configured(updates=user_input)
                try:
                    await async_test_control_connection(host, port)
                except SonanceConnectionError:
                    errors["base"] = "cannot_connect"
                except Exception:
                    _LOGGER.exception("Unexpected error opening control connection")
                    errors["base"] = "unknown"
                else:
                    return self.async_create_entry(
                        title=identity.name, data=user_input
                    )

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )

    @staticmethod
    def async_get_options_flow(config_entry) -> SonanceOptionsFlow:
        return SonanceOptionsFlow()


class SonanceOptionsFlow(OptionsFlowWithReload):
    """Options. Reloads on save via OptionsFlowWithReload.

    Deliberately not paired with entry.add_update_listener. In 2026.9.3 that
    combination is already a hard ValueError at flow creation, not a future
    deprecation -- pairing them does not warn, it breaks the options dialog.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            links = {
                str(source): entity
                for source in range(1, SOURCE_COUNT + 1)
                if (entity := user_input.get(input_link_key(source)))
            }
            return self.async_create_entry(
                data={
                    CONF_MAX_DB: int(user_input[CONF_MAX_DB]),
                    CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
                    CONF_INPUT_LINKS: links,
                }
            )

        # The stored links are a dict keyed by source number; the form wants
        # them flattened back into one field per source.
        current = dict(self.config_entry.options)
        for source, entity in (current.pop(CONF_INPUT_LINKS, None) or {}).items():
            current[input_link_key(int(source))] = entity

        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                _options_schema(), current
            ),
        )

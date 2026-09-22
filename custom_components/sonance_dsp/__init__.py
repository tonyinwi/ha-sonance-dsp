"""The Sonance DSP integration."""

from __future__ import annotations

import logging

from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DEFAULT_HTTP_PORT, DEFAULT_TCP_PORT
from .coordinator import SonanceConfigEntry, SonanceCoordinator
from .http_api import SonanceHttpApi, SonanceHttpError
from .protocol import SonanceConnectionError, SonanceProtocol

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.MEDIA_PLAYER]


async def async_setup_entry(hass: HomeAssistant, entry: SonanceConfigEntry) -> bool:
    """Set up Sonance DSP from a config entry."""
    host: str = entry.data[CONF_HOST]
    port: int = entry.data.get(CONF_PORT, DEFAULT_TCP_PORT)

    api = SonanceHttpApi(async_get_clientsession(hass), host, DEFAULT_HTTP_PORT)
    try:
        identity = await api.identity()
    except SonanceHttpError as err:
        raise ConfigEntryNotReady(f"Could not read amplifier identity: {err}") from err

    client = SonanceProtocol(host, port)
    coordinator = SonanceCoordinator(hass, entry, client, identity, host)

    try:
        await client.connect()
        await coordinator.async_discover()
    except SonanceConnectionError as err:
        await client.disconnect()
        raise ConfigEntryNotReady(f"Could not reach amplifier: {err}") from err

    if not coordinator.groups:
        # Retryable, not terminal. This reads as "no zones configured", but the
        # same symptom is produced by an amplifier that is reachable and simply
        # not answering yet -- and returning False there would strand the entry
        # until someone reloaded it by hand. ConfigEntryNotReady retries with
        # backoff and puts the reason in the UI either way.
        await client.disconnect()
        raise ConfigEntryNotReady(
            f"{identity.name} reported no output groups with channels assigned. "
            "If this persists, assign channels to groups in the amplifier's "
            "web UI."
        )

    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        await coordinator.async_close()
        raise

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SonanceConfigEntry) -> bool:
    """Unload a config entry, releasing the amplifier's single session.

    A leaked socket locks out the next setup entirely -- the amplifier accepts
    one control connection and will not hand it back until this one closes.
    """
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        # runtime_data is normally set, because Home Assistant only unloads an
        # entry whose setup returned True. Not assuming it is set matters
        # anyway: an AttributeError here fails the unload, and a failed unload
        # leaves the entry stuck -- still holding the amplifier's one control
        # session, which is the exact thing this function exists to release.
        coordinator = getattr(entry, "runtime_data", None)
        if coordinator is not None:
            await coordinator.async_close()
        else:
            _LOGGER.debug("Unloading an entry that was never fully set up")
    return unload_ok

"""The Sonance DSP integration.

SCAFFOLD ONLY -- not implemented yet.

Shape this must take (HA 2026.9):

    type SonanceConfigEntry = ConfigEntry[SonanceCoordinator]

    async def async_setup_entry(hass, entry: SonanceConfigEntry) -> bool:
        ...
        await coordinator.async_config_entry_first_refresh()
        entry.runtime_data = coordinator            # NOT hass.data[DOMAIN]
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        return True

Constraints that are easy to get wrong:

* ``async_forward_entry_setups`` (plural). The singular form was removed in 2025.6.
* Tear the single TCP connection down in ``async_unload_entry`` -- the amplifier
  accepts only one session, so a leaked socket locks out the next setup.
* Failure to connect during setup raises ``ConfigEntryNotReady``, not ``False``.
"""

from __future__ import annotations

from homeassistant.const import Platform

PLATFORMS: list[Platform] = [Platform.MEDIA_PLAYER]

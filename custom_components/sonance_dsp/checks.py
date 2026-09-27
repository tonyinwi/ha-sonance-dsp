"""Repair issues for the amplifier settings that power control depends on.

Home Assistant owning power is only safe with three settings on the amplifier,
none of which it can set for itself:

* Auto On method **Power Button** -- otherwise the amp wakes zones by itself;
* every channel's sleep **OFF** -- otherwise it switches zones off by itself;
* every zone's turn-on volume **-70 dB** -- a zone-on plays about a second
  unmuted at that level, whatever its mute (measured, and heard, 2026-09-27).

A factory reset or a change in the web UI undoes any of them silently, so they
are read at setup and daily, and each gets a repair issue while it is wrong.
"""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN, MIN_VOLUME_DB
from .coordinator import SonanceConfigEntry, SonanceCoordinator
from .http_api import SonanceHttpError

_LOGGER = logging.getLogger(__name__)

AUTO_ON_REQUIRED = "Power Button"
SLEEP_OFF = "OFF"
LEARN_MORE_URL = "https://github.com/tonyinwi/ha-sonance-dsp#power"
ISSUES = ("auto_on_method", "channel_sleep", "turn_on_volume")


async def async_check_setup(
    hass: HomeAssistant, entry: SonanceConfigEntry, coordinator: SonanceCoordinator
) -> None:
    """Raise or clear each issue from what the amplifier reports now.

    An unreadable settings page changes nothing: an issue is only raised on a
    reading, and only cleared on one.
    """
    try:
        setup = await coordinator.http.power_setup()
        topology = await coordinator.http.topology()
    except SonanceHttpError as err:
        _LOGGER.debug("Could not read the amplifier's settings to check them: %s", err)
        return

    name = coordinator.identity.name
    method = setup.auto_on_method
    _set(
        hass,
        entry,
        "auto_on_method",
        method is not None and method != AUTO_ON_REQUIRED,
        {"name": name, "method": method or ""},
    )

    sleeping = [
        title
        for title, value in zip(setup.sleep_titles, setup.sleep, strict=False)
        if value.upper() != SLEEP_OFF
    ]
    _set(
        hass,
        entry,
        "channel_sleep",
        bool(sleeping),
        {"name": name, "channels": ", ".join(sleeping)},
    )

    loud: list[str] = []
    for group in coordinator.groups:
        levels = {
            topology.turn_on_volumes[i]
            for i in topology.group_members(group)
            if i < len(topology.turn_on_volumes)
        }
        if any(_not_silent(level) for level in levels):
            zone = topology.group_name(group) or f"Zone {group + 1}"
            loud.append(f"{zone} ({', '.join(sorted(levels))})")
    _set(
        hass,
        entry,
        "turn_on_volume",
        bool(loud),
        {"name": name, "zones": "; ".join(loud), "silent": str(MIN_VOLUME_DB)},
    )


def async_remove_issues(hass: HomeAssistant, entry: SonanceConfigEntry) -> None:
    """Drop every issue for an entry that is being removed."""
    for key in ISSUES:
        ir.async_delete_issue(hass, DOMAIN, f"{key}_{entry.entry_id}")


def _not_silent(level: str) -> bool:
    """Anything but the floor, including LAST and values that do not parse."""
    try:
        return int(level) != MIN_VOLUME_DB
    except ValueError:
        return True


def _set(
    hass: HomeAssistant,
    entry: SonanceConfigEntry,
    key: str,
    active: bool,
    placeholders: dict[str, str],
) -> None:
    issue_id = f"{key}_{entry.entry_id}"
    if not active:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=key,
        translation_placeholders=placeholders,
        learn_more_url=LEARN_MORE_URL,
    )

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

from .const import DOMAIN, GROUP_LETTERS, MAX_VOLUME_DB, MIN_VOLUME_DB
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

    Each check runs only on a reading of its own inputs: an unreadable page, or
    one that answered without the keys, leaves that issue exactly as it was.
    Never raises -- this is advice, and must not take the integration down.
    """
    try:
        await _async_check(hass, entry, coordinator)
    except Exception:
        _LOGGER.exception("Checking the amplifier's power settings failed")


async def _async_check(
    hass: HomeAssistant, entry: SonanceConfigEntry, coordinator: SonanceCoordinator
) -> None:
    name = coordinator.identity.name

    try:
        setup = await coordinator.http.power_setup()
    except SonanceHttpError as err:
        _LOGGER.info("Could not read the amp's Auto On and sleep settings: %s", err)
    else:
        if setup.auto_on_method is not None:
            _set(
                hass,
                entry,
                "auto_on_method",
                setup.auto_on_method != AUTO_ON_REQUIRED,
                {"name": name, "method": setup.auto_on_method},
            )
        if setup.sleep:
            titles = setup.sleep_titles
            if len(titles) != len(setup.sleep):
                titles = [f"channel {i + 1}" for i in range(len(setup.sleep))]
            sleeping = [
                title
                for title, value in zip(titles, setup.sleep, strict=True)
                if value.upper() != SLEEP_OFF
            ]
            _set(
                hass,
                entry,
                "channel_sleep",
                bool(sleeping),
                {"name": name, "channels": ", ".join(sleeping)},
            )

    try:
        topology = await coordinator.http.topology()
    except SonanceHttpError as err:
        _LOGGER.info("Could not read the amplifier's turn-on volumes: %s", err)
        return
    if not topology.turn_on_volumes:
        return
    loud: list[str] = []
    for group in coordinator.groups:
        levels = {
            topology.turn_on_volumes[i]
            for i in topology.group_members(group)
            if i < len(topology.turn_on_volumes)
        }
        if any(_not_silent(level) for level in levels):
            zone = topology.group_name(group) or f"Zone {GROUP_LETTERS[group]}"
            shown = ", ".join(sorted(_label(level) for level in levels))
            loud.append(f"{zone} ({shown})")
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


def _label(level: str) -> str:
    """LAST is stored as a value above the device range (13, inferred)."""
    try:
        return "LAST" if int(level) > MAX_VOLUME_DB else f"{int(level)} dB"
    except ValueError:
        return level or "?"


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
    existing = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    if (
        existing is not None
        and existing.dismissed_version is not None
        and existing.translation_placeholders != placeholders
    ):
        # Dismissed for one set of zones or channels; a different set is news.
        ir.async_delete_issue(hass, DOMAIN, issue_id)
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        is_persistent=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key=key,
        translation_placeholders=placeholders,
        learn_more_url=LEARN_MORE_URL,
    )

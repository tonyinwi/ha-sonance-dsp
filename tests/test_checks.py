"""Tests for the repair issues on the amplifier settings power control depends on."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.sonance_dsp.checks import async_check_setup, async_remove_issues
from custom_components.sonance_dsp.const import DOMAIN
from custom_components.sonance_dsp.coordinator import holds_store_key
from custom_components.sonance_dsp.http_api import (
    AmplifierIdentity,
    PowerSetup,
    SonanceHttpError,
    Topology,
)
from custom_components.sonance_dsp.protocol import GroupState

IDENTITY = AmplifierIdentity(
    serial="SERIAL123", name="Back Yard", model="DSP8-130 MKII", firmware="V2.2.8130"
)
TITLES = [f"{n} {side}" for n in range(1, 5) for side in ("LEFT", "RIGHT")]
GOOD = PowerSetup(auto_on_method="Power Button", sleep=["OFF"] * 8, sleep_titles=TITLES)
TOPOLOGY = Topology(
    output_names=["Patio L", "Patio R", "Deck L", "Deck R",
                  "Lawn L", "Lawn R", "Output 4L", "Output 4R"],
    input_names=["In1 L", "In1 R"],
    output_groups=["a", "a", "b", "b", "c", "c", "d", "d"],
    turn_on_volumes=["-70"] * 8,
)


def make(hass: HomeAssistant, setup: PowerSetup = GOOD, topology: Topology = TOPOLOGY):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=IDENTITY.serial, data={})
    entry.add_to_hass(hass)
    coordinator = MagicMock()
    coordinator.identity = IDENTITY
    coordinator.groups = [0, 1, 2, 3]
    coordinator.http.power_setup = AsyncMock(return_value=setup)
    coordinator.http.topology = AsyncMock(return_value=topology)
    return entry, coordinator


def issue(
    hass: HomeAssistant, key: str, entry: MockConfigEntry
) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, f"{key}_{entry.entry_id}")


async def test_the_recommended_setup_raises_nothing(hass: HomeAssistant) -> None:
    entry, c = make(hass)

    await async_check_setup(hass, entry, c)

    for key in ("auto_on_method", "channel_sleep", "turn_on_volume"):
        assert issue(hass, key, entry) is None


async def test_auto_on_other_than_power_button_is_an_issue(hass: HomeAssistant) -> None:
    """Audio sense wakes zones by itself: the 2am failure."""
    entry, c = make(hass, replace(GOOD, auto_on_method="Audio"))

    await async_check_setup(hass, entry, c)

    found = issue(hass, "auto_on_method", entry)
    assert found is not None
    assert found.translation_placeholders == {"name": "Back Yard", "method": "Audio"}
    assert found.severity is ir.IssueSeverity.WARNING
    assert found.is_fixable is False


async def test_sleep_on_any_channel_is_an_issue(hass: HomeAssistant) -> None:
    sleep = ["After 15 Min", "After 15 Min"] + ["OFF"] * 6
    entry, c = make(hass, replace(GOOD, sleep=sleep))

    await async_check_setup(hass, entry, c)

    found = issue(hass, "channel_sleep", entry)
    assert found is not None
    assert found.translation_placeholders["channels"] == "1 LEFT, 1 RIGHT"


@pytest.mark.parametrize(
    ("level", "shown"),
    [("-27", "-27 dB"), ("13", "LAST"), ("LAST", "LAST"), ("", "?")],
)
async def test_a_turn_on_volume_other_than_the_floor_is_an_issue(
    hass: HomeAssistant, level: str, shown: str
) -> None:
    """Including LAST (stored as 13, inferred) and anything unparseable."""
    volumes = ["-70"] * 2 + [level] * 2 + ["-70"] * 4
    entry, c = make(hass, topology=replace(TOPOLOGY, turn_on_volumes=volumes))

    await async_check_setup(hass, entry, c)

    found = issue(hass, "turn_on_volume", entry)
    assert found is not None
    assert found.translation_placeholders["zones"] == f"Deck ({shown})"
    assert found.translation_placeholders["silent"] == "-70"


async def test_a_fixed_setting_clears_its_issue(hass: HomeAssistant) -> None:
    entry, c = make(hass, replace(GOOD, auto_on_method="Audio"))
    await async_check_setup(hass, entry, c)
    assert issue(hass, "auto_on_method", entry) is not None

    c.http.power_setup.return_value = GOOD
    await async_check_setup(hass, entry, c)

    assert issue(hass, "auto_on_method", entry) is None


async def test_an_unreadable_settings_page_changes_nothing(hass: HomeAssistant) -> None:
    """An issue is only raised on a reading, and only cleared on one."""
    entry, c = make(hass, replace(GOOD, auto_on_method="Audio"))
    await async_check_setup(hass, entry, c)

    c.http.power_setup.side_effect = SonanceHttpError("down")
    await async_check_setup(hass, entry, c)

    assert issue(hass, "auto_on_method", entry) is not None


async def test_an_unknown_auto_on_method_is_not_an_issue(hass: HomeAssistant) -> None:
    """Not read is not wrong."""
    entry, c = make(hass, replace(GOOD, auto_on_method=None))

    await async_check_setup(hass, entry, c)

    assert issue(hass, "auto_on_method", entry) is None


async def test_removing_the_entry_removes_its_issues(hass: HomeAssistant) -> None:
    entry, c = make(
        hass,
        PowerSetup("Audio", ["After 3 HRS"] * 8, TITLES),
        replace(TOPOLOGY, turn_on_volumes=["-27"] * 8),
    )
    await async_check_setup(hass, entry, c)

    async_remove_issues(hass, entry)

    for key in ("auto_on_method", "channel_sleep", "turn_on_volume"):
        assert issue(hass, key, entry) is None


# ---------------------------------------------------------------------------
# Wiring: checked at setup, and again every day
# ---------------------------------------------------------------------------


async def test_setup_checks_now_and_daily(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=IDENTITY.serial, data={CONF_HOST: "192.0.2.10"}
    )
    entry.add_to_hass(hass)
    key = holds_store_key(entry.entry_id)
    hass_storage[key] = {  # a hold saved before a restart
        "version": 1, "minor_version": 1, "key": key,
        "data": {"held_level": {"0": -35}, "mute_owed": []},
    }
    power_setup = AsyncMock(return_value=replace(GOOD, auto_on_method="Audio"))
    group = GroupState(group=0, volume_db=-70, muted=True, source_name="In1 L")
    with (
        patch("custom_components.sonance_dsp.SonanceHttpApi.identity",
              AsyncMock(return_value=IDENTITY)),
        patch("custom_components.sonance_dsp.coordinator.SonanceHttpApi.power_setup",
              power_setup),
        patch("custom_components.sonance_dsp.coordinator.SonanceHttpApi.topology",
              AsyncMock(return_value=TOPOLOGY)),
        patch("custom_components.sonance_dsp.coordinator.SonanceHttpApi.group_power",
              AsyncMock(return_value={0: True, 1: True, 2: True, 3: True})),
        patch("custom_components.sonance_dsp.SonanceProtocol.connect", AsyncMock()),
        patch("custom_components.sonance_dsp.SonanceProtocol.disconnect", AsyncMock()),
        patch("custom_components.sonance_dsp.SonanceProtocol.discover_groups",
              AsyncMock(return_value=[0, 1, 2, 3])),
        patch("custom_components.sonance_dsp.SonanceProtocol.read_group",
              AsyncMock(return_value=group)),
        patch("custom_components.sonance_dsp.SonanceProtocol.get_amp_power",
              AsyncMock(return_value=True)),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert issue(hass, "auto_on_method", entry) is not None
        assert entry.runtime_data.held_level(0) == -35  # loaded at setup
        calls = power_setup.await_count

        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(hours=25))
        await hass.async_block_till_done()
        assert power_setup.await_count > calls

        assert await hass.config_entries.async_unload(entry.entry_id)
        assert issue(hass, "auto_on_method", entry) is not None  # unload keeps it

        await hass.config_entries.async_remove(entry.entry_id)
        assert issue(hass, "auto_on_method", entry) is None  # removal drops it
        assert key not in hass_storage  # and the saved zone state



# ---------------------------------------------------------------------------
# Failure paths: never take setup down, never clear on a non-reading
# ---------------------------------------------------------------------------


async def test_an_unexpected_error_is_logged_not_raised(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    entry, c = make(hass)
    c.http.power_setup.side_effect = TypeError("malformed")

    await async_check_setup(hass, entry, c)

    assert "Checking the amplifier's power settings failed" in caplog.text


@pytest.mark.parametrize(
    "answer",
    [
        PowerSetup(auto_on_method=None, sleep=[], sleep_titles=[]),
    ],
)
async def test_an_answer_without_the_keys_keeps_existing_issues(
    hass: HomeAssistant, answer: PowerSetup
) -> None:
    """Not read is not fixed."""
    entry, c = make(
        hass,
        PowerSetup("Audio", ["After 3 HRS"] * 8, TITLES),
        replace(TOPOLOGY, turn_on_volumes=["-27"] * 8),
    )
    await async_check_setup(hass, entry, c)

    c.http.power_setup.return_value = answer
    c.http.topology.return_value = replace(TOPOLOGY, turn_on_volumes=[])
    await async_check_setup(hass, entry, c)

    for key in ("auto_on_method", "channel_sleep", "turn_on_volume"):
        assert issue(hass, key, entry) is not None, key


async def test_one_page_failing_does_not_skip_the_other(hass: HomeAssistant) -> None:
    entry, c = make(hass, replace(GOOD, auto_on_method="Audio"))
    c.http.topology.side_effect = SonanceHttpError("down")

    await async_check_setup(hass, entry, c)

    assert issue(hass, "auto_on_method", entry) is not None


async def test_short_titles_still_report_sleeping_channels(hass: HomeAssistant) -> None:
    entry, c = make(hass, PowerSetup("Power Button", ["After 3 HRS"] * 8, []))

    await async_check_setup(hass, entry, c)

    found = issue(hass, "channel_sleep", entry)
    assert found is not None
    assert found.translation_placeholders["channels"].startswith("channel 1, channel 2")


async def test_issues_survive_a_restart(hass: HomeAssistant) -> None:
    entry, c = make(hass, replace(GOOD, auto_on_method="Audio"))

    await async_check_setup(hass, entry, c)

    assert issue(hass, "auto_on_method", entry).is_persistent is True


async def test_a_dismissed_issue_returns_when_more_zones_join_it(
    hass: HomeAssistant,
) -> None:
    volumes = ["-27", "-27"] + ["-70"] * 6
    entry, c = make(hass, topology=replace(TOPOLOGY, turn_on_volumes=volumes))
    await async_check_setup(hass, entry, c)
    ir.async_get(hass).async_ignore(DOMAIN, f"turn_on_volume_{entry.entry_id}", True)
    assert issue(hass, "turn_on_volume", entry).dismissed_version is not None

    c.http.topology.return_value = replace(
        TOPOLOGY, turn_on_volumes=["-27"] * 4 + ["-70"] * 4
    )
    await async_check_setup(hass, entry, c)

    assert issue(hass, "turn_on_volume", entry).dismissed_version is None


async def test_a_dismissed_issue_stays_dismissed_when_nothing_changed(
    hass: HomeAssistant,
) -> None:
    volumes = ["-27", "-27"] + ["-70"] * 6
    entry, c = make(hass, topology=replace(TOPOLOGY, turn_on_volumes=volumes))
    await async_check_setup(hass, entry, c)
    ir.async_get(hass).async_ignore(DOMAIN, f"turn_on_volume_{entry.entry_id}", True)

    await async_check_setup(hass, entry, c)

    assert issue(hass, "turn_on_volume", entry).dismissed_version is not None


async def test_general_settings_failing_does_not_skip_the_turn_on_check(
    hass: HomeAssistant,
) -> None:
    entry, c = make(hass, topology=replace(TOPOLOGY, turn_on_volumes=["-27"] * 8))
    c.http.power_setup.side_effect = SonanceHttpError("down")

    await async_check_setup(hass, entry, c)

    assert issue(hass, "turn_on_volume", entry) is not None

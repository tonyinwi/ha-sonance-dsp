"""Tests for zone power, built on a fake amplifier that behaves like the real one.

The fake encodes what was MEASURED on a DSP 8-130 MKII in Power Button mode,
rather than what would be convenient:

* switching a zone ON clears its mute -- not instantly but a moment later
* standby and wake do not clear mute, and an off zone still reports it
* a zone's on/off flag survives standby, and a wake brings back every flagged zone
* a wake takes a while, and commands sent before it finishes are DROPPED
  (they would echo success on the real device; here they simply vanish)
* switching every zone off does not put the amp in standby by itself

And it can be told to misbehave in the ways the real one has been seen to, or
might: lose a command, leave a query unanswered, refuse writes in standby.

Things the fake has to assume because they were never measured, each of which
the code under test is written to be right about either way:

* a zone-on sent to a zone that is already on resets its volume and clears its
  mute just as it does from off (pessimistic)
* a mute sent straight after a zone-on cancels the pending clear -- or, with
  ``clear_overrides_mute``, does not, and the clear lands on top of it. The
  amp was measured doing neither exactly: it clears at ~0.2 s and re-applies
  the mute itself at ~1.05 s. The two modes bracket that.
* standby accepts and echoes zone commands, unless told otherwise

Because the fake drops mid-boot commands and clears mute late, a regression
that restores mute before the wake has finished, or only once, fails here
instead of silently passing.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from homeassistant.components.media_player import MediaPlayerState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.sonance_dsp import coordinator as coord_mod
from custom_components.sonance_dsp.const import DOMAIN
from custom_components.sonance_dsp.coordinator import (
    SonanceCoordinator,
    SonanceData,
    async_remove_holds,
    holds_store_key,
)
from custom_components.sonance_dsp.http_api import (
    AmplifierIdentity,
    SonanceHttpError,
    Topology,
)
from custom_components.sonance_dsp.media_player import SonanceZone
from custom_components.sonance_dsp.protocol import (
    GroupState,
    SonanceConnectionError,
    SonanceNotSentError,
)

IDENTITY = AmplifierIdentity(
    serial="SERIAL123", name="Back Yard", model="DSP8-130 MKII", firmware="V2.2.8130"
)
TOPOLOGY = Topology(
    output_names=["A L", "A R", "B L", "B R", "C L", "C R", "D L", "D R"],
    input_names=["In1 L", "In1 R", "In2 L", "In2 R"],
    output_groups=["a", "a", "b", "b", "c", "c", "d", "d"],
)


class FakeAmp:
    """Stateful stand-in for the amplifier, with its measured quirks."""

    TURN_ON_VOLUME = -27
    # Operations after a zone-on before its mute clear lands. The real delay
    # is "within about a second"; counting operations keeps tests deterministic.
    MUTE_CLEAR_LAG = 3

    def __init__(
        self,
        *,
        master: bool = True,
        boot_polls: int = 3,
        standby_accepts_writes: bool = True,
        standby_echoes_writes: bool = True,
        clear_overrides_mute: bool = False,
    ) -> None:
        self.master = master
        self.boot_polls = boot_polls
        self.standby_accepts_writes = standby_accepts_writes
        self.standby_echoes_writes = standby_echoes_writes
        self.clear_overrides_mute = clear_overrides_mute
        self._booting = 0
        self.group_power = {0: True, 1: True, 2: True, 3: True}
        self.mute = {0: False, 1: False, 2: False, 3: False}
        self.volume = {0: -27, 1: -27, 2: -27, 3: -49}
        self.log: list[str] = []
        self.dropped: list[str] = []
        self.http_up = True
        self.unanswered_amp_power = 0
        self.unanswered_mute = 0
        self.unanswered_volume = 0
        self.http_hang = False
        self.http_calls = 0
        self._lose: list[str] = []
        self._lose_echo: list[str] = []
        self._fail: list[str] = []
        self._not_sent: list[str] = []
        self.revive_on_wake: set[int] = set()
        # Unmeasured: whether a wake resets zone volumes. Off by default.
        self.wake_applies_turn_on_volume = False
        self.read_group_failures = 0
        self._pending_clear: dict[int, int] = {}
        self._pending_turn_on: dict[int, int] = {}
        self.turn_on_volume = self.TURN_ON_VOLUME

    # --- test controls -------------------------------------------------------

    def lose_next(self, entry: str) -> None:
        """Drop the next command logged as ``entry`` without applying it."""
        self._lose.append(entry)

    def lose_echo_next(self, entry: str) -> None:
        """Apply the next ``entry`` but never answer it: the late-echo case."""
        self._lose_echo.append(entry)

    def not_sent_next(self, entry: str) -> None:
        """Fail the next ``entry`` before it reaches the amp: no connection."""
        self._not_sent.append(entry)

    def fail_next(self, entry: str, times: int = 1) -> None:
        """Refuse ``entry`` with no reply and no effect, while the amp is awake."""
        self._fail.extend([entry] * times)

    def _no_echo(self, entry: str) -> None:
        if entry in self._lose_echo:
            self._lose_echo.remove(entry)
            raise SonanceConnectionError(f"no reply to {entry}")

    def settle(self) -> None:
        """Let every pending mute clear land, as a second of real time would."""
        for group in self._pending_clear:
            self.mute[group] = False
        self._pending_clear.clear()
        for group in self._pending_turn_on:
            self.volume[group] = self.turn_on_volume
        self._pending_turn_on.clear()

    async def power_up(self) -> None:
        """A zone's power-up passing: what the amp does within ~1 s lands."""
        self.settle()

    def live(self, group: int) -> bool:
        """Is this zone actually producing output?"""
        return self.master and not self._booting and self.group_power[group]

    def _tick(self, entry: str) -> None:
        self.log.append(entry)
        for group in list(self._pending_clear):
            self._pending_clear[group] -= 1
            if self._pending_clear[group] <= 0:
                del self._pending_clear[group]
                self.mute[group] = False
        for group in list(self._pending_turn_on):
            self._pending_turn_on[group] -= 1
            if self._pending_turn_on[group] <= 0:
                del self._pending_turn_on[group]
                self.volume[group] = self.turn_on_volume

    def _ignored(self, entry: str) -> bool:
        """Is this write lost: mid-boot, refused in standby, or told to be?"""
        if entry in self._not_sent:
            self._not_sent.remove(entry)
            self.log.pop()  # it never reached the amp
            raise SonanceNotSentError(f"could not connect to send {entry}")
        if entry in self._fail and self.master and not self._booting:
            self._fail.remove(entry)
            self.dropped.append(entry)
            raise SonanceConnectionError(f"no reply to {entry}")
        if not self.master and not self._booting and not self.standby_echoes_writes:
            # What the protocol layer raises when a write gets no reply.
            self.dropped.append(entry)
            raise SonanceConnectionError(f"no reply to {entry}")
        if entry in self._lose:
            self._lose.remove(entry)
            self.dropped.append(entry)
            return True
        if self._booting or (not self.master and not self.standby_accepts_writes):
            self.dropped.append(entry)
            return True
        return False

    # --- TCP ----------------------------------------------------------------

    async def get_amp_power(self) -> bool | None:
        self._tick("q:master")
        if self.unanswered_amp_power:
            self.unanswered_amp_power -= 1
            return None
        if self._booting:
            self._booting -= 1
            if not self._booting:
                self.master = True
                for group in self.revive_on_wake:
                    self.group_power[group] = True
                if self.wake_applies_turn_on_volume:
                    for group in self.volume:
                        self.volume[group] = self.turn_on_volume
            return False
        return self.master

    async def set_amp_power(self, on: bool) -> None:
        entry = f"amp:{'on' if on else 'off'}"
        self._tick(entry)
        if on and not self.master:
            self._booting = self.boot_polls
        elif not on:
            self.master = False
        self._no_echo(entry)

    async def set_group_power(self, group: int, on: bool) -> None:
        entry = f"group{group}:{'on' if on else 'off'}"
        self._tick(entry)
        if self._ignored(entry):
            return
        was_on = self.group_power[group]
        self.group_power[group] = on
        if on and not was_on:
            # Measured from off: a moment later the zone's turn-on volume
            # lands, over any volume sent before it, and its mute clears. To a
            # zone already on, a zone-on does nothing (measured 2026-09-27).
            self._pending_turn_on[group] = self.MUTE_CLEAR_LAG
            self._pending_clear[group] = self.MUTE_CLEAR_LAG
        self._no_echo(entry)

    async def set_mute(self, group: int, mute: bool) -> None:
        entry = f"mute{group}:{'on' if mute else 'off'}"
        self._tick(entry)
        if self._ignored(entry):
            return
        if not self.clear_overrides_mute:
            self._pending_clear.pop(group, None)
        self.mute[group] = mute
        self._no_echo(entry)

    async def set_group_power_muted(self, group: int) -> None:
        """As the protocol does it: the mute goes out even if the zone-on fails."""
        error: SonanceConnectionError | None = None
        try:
            await self.set_group_power(group, True)
        except SonanceNotSentError:
            raise
        except SonanceConnectionError as err:
            error = err
        await self.set_mute(group, True)
        if error is not None:
            raise error

    async def set_volume(self, group: int, db: int) -> None:
        entry = f"vol{group}:{db}"
        self._tick(entry)
        if self._ignored(entry):
            return
        self.volume[group] = db
        self.mute[group] = False  # measured: ANY volume change un-mutes
        self._no_echo(entry)

    async def volume_up(self, group: int) -> None:
        self._tick(f"vol{group}:up")
        self.volume[group] = min(12, self.volume[group] + 1)
        self.mute[group] = False

    async def volume_down(self, group: int) -> None:
        self._tick(f"vol{group}:down")
        self.volume[group] = max(-70, self.volume[group] - 1)
        self.mute[group] = False

    async def set_source(self, group: int, source: int) -> None:
        entry = f"src{group}:{source}"
        self._tick(entry)
        self._ignored(entry)

    async def get_mute(self, group: int) -> bool | None:
        self._tick(f"q:mute{group}")
        if self.unanswered_mute:
            self.unanswered_mute -= 1
            return None
        return self.mute[group]

    async def get_volume(self, group: int) -> int | None:
        self._tick(f"q:vol{group}")
        if self.unanswered_volume:
            self.unanswered_volume -= 1
            return None
        return self.volume[group]

    async def disconnect(self) -> None:
        self.log.append("disconnect")

    async def read_group(self, group: int) -> GroupState:
        self._tick(f"q:group{group}")
        if self.read_group_failures:
            self.read_group_failures -= 1
            raise SonanceConnectionError("no reply")
        return GroupState(
            group=group,
            volume_db=self.volume[group],
            muted=self.mute[group],
            source_name="In1 L",
        )

    # --- HTTP ---------------------------------------------------------------

    async def http_group_power(self) -> dict[int, bool]:
        self.http_calls += 1
        if self.http_hang:
            await asyncio.sleep(3600)
        if not self.http_up:
            raise SonanceHttpError("down")
        return dict(self.group_power)


@pytest.fixture(autouse=True)
def fast_timing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(coord_mod, "WAKE_POLL_INTERVAL", 0)
    monkeypatch.setattr(coord_mod, "WAKE_TIMEOUT", 0.5)
    monkeypatch.setattr(coord_mod, "POWER_CONFIRM_INTERVAL", 0)
    monkeypatch.setattr(coord_mod, "POWER_CONFIRM_TIMEOUT", 0.05)
    monkeypatch.setattr(coord_mod, "WAKE_RETRY_HOLDOFF", 60.0)
    monkeypatch.setattr(coord_mod, "MUTE_VERIFY_DELAYS", (0, 0, 0, 0))
    monkeypatch.setattr(coord_mod, "MUTE_VERIFY_DELAYS_AFTER_WAKE", (0,) * 6)
    monkeypatch.setattr(coord_mod, "VOLUME_RESTORE_AFTER", 0)
    monkeypatch.setattr(coord_mod, "VOLUME_CONFIRM_DELAYS", (0, 0, 0))
    monkeypatch.setattr(coord_mod, "AMP_POWER_READ_INTERVAL", 0)


def make(
    hass: HomeAssistant, amp: FakeAmp, entry: MockConfigEntry | None = None
) -> SonanceCoordinator:
    if entry is None:
        entry = MockConfigEntry(domain=DOMAIN, unique_id=IDENTITY.serial, data={})
        entry.add_to_hass(hass)
    client = MagicMock()
    for name in ("get_amp_power", "set_amp_power", "set_group_power",
                 "set_group_power_muted", "set_mute", "set_volume", "set_source",
                 "volume_up", "volume_down",
                 "get_mute", "get_volume", "read_group", "disconnect"):
        setattr(client, name, getattr(amp, name))
    c = SonanceCoordinator(hass, entry, client, IDENTITY, "192.0.2.10")
    c.http = MagicMock()
    c.http.group_power = amp.http_group_power
    c._async_wait_for_power_up = amp.power_up
    c._topology = TOPOLOGY
    c.groups = [0, 1, 2, 3]
    c.data = SonanceData(
        identity=IDENTITY,
        topology=TOPOLOGY,
        groups={
            g: GroupState(group=g, volume_db=amp.volume[g], muted=amp.mute[g],
                          source_name="In1 L")
            for g in range(4)
        },
        group_power=dict(amp.group_power),
        amp_power=amp.master,
    )
    return c


async def wait_for_lock(c: SonanceCoordinator) -> None:
    """Until a command holds the lock; fails fast rather than hanging the suite."""
    async with asyncio.timeout(2):
        while not c.command_lock.locked():
            await asyncio.sleep(0)


async def wait_for_unlock(c: SonanceCoordinator) -> None:
    """Until the lock is free AND nothing is queued on it.

    Checking locked() alone returns in the gap between one holder releasing
    and the next waiter taking it -- before a queued command has run at all.
    """
    lock = c.command_lock
    async with asyncio.timeout(2):
        await asyncio.sleep(0)
        while lock.locked() or getattr(lock, "_waiters", None):
            await asyncio.sleep(0.001)


# ---------------------------------------------------------------------------
# Turning a zone on
# ---------------------------------------------------------------------------


async def test_turn_on_restores_a_muted_zone(hass: HomeAssistant) -> None:
    """A zone muted before it went off comes back muted.

    Switching a zone on clears its mute, so without the restore a muted zone
    would come back unmuted -- the exact failure HA owning power exists to stop.
    """
    amp = FakeAmp()
    amp.group_power[0] = False
    amp.mute[0] = True
    c = make(hass, amp)

    await c.async_turn_on(0)
    amp.settle()

    assert amp.group_power[0] is True
    assert amp.mute[0] is True
    # Mute is read BEFORE switching on (the amp still holds it while off)
    # and put back AFTER, because switching on is what clears it.
    order = [amp.log.index(e) for e in ("q:mute0", "group0:on", "mute0:on")]
    read, on, restored = order
    assert read < on < restored


async def test_turn_on_leaves_an_unmuted_zone_unmuted(hass: HomeAssistant) -> None:
    amp = FakeAmp()
    amp.group_power[0] = False
    c = make(hass, amp)

    await c.async_turn_on(0)

    assert amp.mute[0] is False
    assert "mute0:on" not in amp.log


async def test_turn_on_puts_back_the_level_the_zone_had(hass: HomeAssistant) -> None:
    """The turn-on volume is only the power-up level; the zone's own comes back."""
    amp = FakeAmp()
    amp.group_power[3] = False
    c = make(hass, amp)

    await c.async_turn_on(3, ceiling_db=0)
    amp.settle()

    assert amp.volume[3] == -49
    assert c.data.groups[3].volume_db == -49
    assert c.data.group_power[3] is True


async def test_turn_on_from_standby_waits_for_the_boot(hass: HomeAssistant) -> None:
    """Nothing but status queries may be sent until the amp reports On.

    On the real device commands sent mid-boot echo success and are silently
    dropped. The fake drops them too, so sending early fails this test.
    """
    amp = FakeAmp(master=False, boot_polls=3)
    for g in amp.group_power:
        amp.group_power[g] = False
    amp.mute[0] = True
    c = make(hass, amp)

    await c.async_turn_on(0)
    amp.settle()

    assert amp.dropped == [], f"commands were sent mid-boot and lost: {amp.dropped}"
    assert amp.master is True
    assert amp.group_power[0] is True
    assert amp.mute[0] is True
    wake = amp.log.index("amp:on")
    first_write_after = next(
        i for i, e in enumerate(amp.log) if i > wake and not e.startswith("q:")
    )
    assert all(e.startswith("q:") for e in amp.log[wake + 1:first_write_after])


async def test_turning_on_several_zones_shares_one_wake(hass: HomeAssistant) -> None:
    """A scene switching zones on together must not send a power-on each."""
    amp = FakeAmp(master=False, boot_polls=3)
    for g in amp.group_power:
        amp.group_power[g] = False
    c = make(hass, amp)

    await c.async_turn_on(0)
    await c.async_turn_on(1)
    await c.async_turn_on(2)

    assert amp.log.count("amp:on") == 1
    assert amp.dropped == []


async def test_wake_that_never_finishes_raises(hass: HomeAssistant) -> None:
    amp = FakeAmp(master=False, boot_polls=10**9)  # never finishes
    for g in amp.group_power:
        amp.group_power[g] = False
    c = make(hass, amp)

    with pytest.raises(HomeAssistantError) as err:
        await c.async_turn_on(0)
    assert err.value.translation_key == "amp_did_not_wake"
    # And it did not go on to send the zone command into a booting amp.
    assert "group0:on" not in amp.log


# ---------------------------------------------------------------------------
# Turning a zone off
# ---------------------------------------------------------------------------


async def test_turn_off_keeps_the_amp_on_while_other_zones_play(
    hass: HomeAssistant,
) -> None:
    amp = FakeAmp()
    c = make(hass, amp)

    await c.async_turn_off(0)

    assert amp.group_power[0] is False
    assert amp.master is True
    assert "amp:off" not in amp.log


async def test_turning_off_the_last_zone_puts_the_amp_in_standby(
    hass: HomeAssistant,
) -> None:
    """The amp does not do this itself -- measured with all four zones off."""
    amp = FakeAmp()
    for g in (1, 2, 3):
        amp.group_power[g] = False
    c = make(hass, amp)

    await c.async_turn_off(0)

    assert amp.master is False
    assert c.data.amp_power is False


async def test_no_standby_when_the_status_page_is_down(hass: HomeAssistant) -> None:
    """Without it there is no knowing whether another zone is on."""
    amp = FakeAmp()
    for g in (1, 2, 3):
        amp.group_power[g] = False
    amp.http_up = False
    c = make(hass, amp)

    await c.async_turn_off(0)

    assert amp.group_power[0] is False
    assert amp.master is True


async def test_unconfirmed_power_change_is_retried_once(
    hass: HomeAssistant,
) -> None:
    amp = FakeAmp()
    c = make(hass, amp)
    real = amp.set_group_power
    calls = {"n": 0}

    async def flaky(group: int, on: bool) -> None:
        calls["n"] += 1
        amp.log.append(f"group{group}:{'on' if on else 'off'}")
        if calls["n"] > 1:   # the first command is lost
            amp.group_power[group] = on
    c.client.set_group_power = flaky

    await c.async_turn_off(0)

    assert calls["n"] == 2
    assert amp.group_power[0] is False
    c.client.set_group_power = real


async def test_power_change_that_never_shows_raises(hass: HomeAssistant) -> None:
    amp = FakeAmp()
    c = make(hass, amp)

    async def ignored(group: int, on: bool) -> None:
        return None
    c.client.set_group_power = ignored

    with pytest.raises(HomeAssistantError) as err:
        await c.async_turn_off(0)
    assert err.value.translation_key == "zone_power_not_confirmed"


# ---------------------------------------------------------------------------
# What a zone shows
# ---------------------------------------------------------------------------


def zone(
    c: SonanceCoordinator, hass: HomeAssistant, group: int = 0, max_db: int = 0
) -> SonanceZone:
    z = SonanceZone(c, group, max_db, {"1": "media_player.streamer"})
    z.hass = hass
    z.entity_id = f"media_player.zone_{group}"
    return z


async def test_zone_is_off_when_the_amp_is_in_standby(hass: HomeAssistant) -> None:
    """A zone's flag survives standby, so the flag alone would show it on."""
    amp = FakeAmp()
    c = make(hass, amp)
    c.data.amp_power = False
    c.data.group_power[0] = True
    assert zone(c, hass).state is MediaPlayerState.OFF


async def test_zone_is_off_when_its_group_is_off(hass: HomeAssistant) -> None:
    amp = FakeAmp()
    c = make(hass, amp)
    c.data.group_power[0] = False
    assert zone(c, hass).state is MediaPlayerState.OFF


async def test_zone_state_is_unknown_without_the_status_page(
    hass: HomeAssistant,
) -> None:
    """A zone that answers queries is not thereby on -- an off zone answers too."""
    amp = FakeAmp()
    c = make(hass, amp)
    c.data.group_power = {}
    assert zone(c, hass).state is None


async def test_an_off_zone_does_not_show_its_sources_track(hass: HomeAssistant) -> None:
    hass.states.async_set("media_player.streamer", "playing", {"media_title": "X"})
    amp = FakeAmp()
    c = make(hass, amp)
    c.data.group_power[0] = False
    z = zone(c, hass)
    z._resubscribe()
    assert z.state is MediaPlayerState.OFF
    assert z.media_title is None


async def test_a_powered_zone_still_mirrors(hass: HomeAssistant) -> None:
    hass.states.async_set("media_player.streamer", "playing", {"media_title": "X"})
    amp = FakeAmp()
    c = make(hass, amp)
    z = zone(c, hass)
    z._resubscribe()
    assert z.state is MediaPlayerState.PLAYING
    assert z.media_title == "X"


# ---------------------------------------------------------------------------
# Mute restore, under the faults the review found
# ---------------------------------------------------------------------------


async def test_mute_survives_a_retried_switch_on(hass: HomeAssistant) -> None:
    """Every zone-on clears the mute, so a retry's must be followed by one too.

    The first zone-on is lost, so the confirm fails and it is sent again. If
    the restore only followed the first, the retry would clear it a moment
    later -- exactly when the retry was needed.
    """
    amp = FakeAmp()
    amp.group_power[0] = False
    amp.mute[0] = True
    amp.lose_next("group0:on")
    c = make(hass, amp)

    await c.async_turn_on(0)
    amp.settle()

    assert amp.log.count("group0:on") == 2
    assert amp.group_power[0] is True
    assert amp.mute[0] is True
    assert c.data.groups[0].muted is True


async def test_cancelled_turn_on_still_restores_the_mute(hass: HomeAssistant) -> None:
    """A caller that goes away after the zone-on must not strand the zone.

    The cancel arrives while the zone is on and its mute not yet verified; the
    sequence has to carry on to the end regardless.
    """
    amp = FakeAmp(clear_overrides_mute=True)
    amp.group_power[0] = False
    amp.mute[0] = True
    c = make(hass, amp)
    real = amp.set_group_power_muted
    switched_on = asyncio.Event()
    release = asyncio.Event()

    async def slow(group: int) -> None:
        await real(group)
        switched_on.set()
        await release.wait()

    c.client.set_group_power_muted = slow
    task = asyncio.ensure_future(c.async_turn_on(0))
    await switched_on.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    await wait_for_unlock(c)
    amp.settle()

    assert amp.mute[0] is True
    assert c.data.group_power[0] is True


@pytest.mark.parametrize("master", [True, False], ids=["awake", "standby"])
async def test_turn_on_cancelled_before_anything_is_sent_does_nothing(
    hass: HomeAssistant, master: bool
) -> None:
    """Queued behind another command and then cancelled: nothing is sent at all."""
    amp = FakeAmp(master=master)
    amp.group_power = {0: False, 1: False, 2: True, 3: False}
    c = make(hass, amp)

    async with c.command_lock:
        task = asyncio.ensure_future(c.async_turn_on(0))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    await wait_for_unlock(c)

    assert amp.log == []


async def test_turn_on_cancelled_during_the_amp_read_sends_nothing(
    hass: HomeAssistant,
) -> None:
    amp = FakeAmp(master=False)
    amp.group_power = {0: False, 1: False, 2: True, 3: False}
    c = make(hass, amp)
    real = amp.get_amp_power
    reading = asyncio.Event()
    release = asyncio.Event()

    async def slow() -> bool | None:
        reading.set()
        await release.wait()
        return await real()

    c.client.get_amp_power = slow
    task = asyncio.ensure_future(c.async_turn_on(0))
    await reading.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    await wait_for_unlock(c)

    assert [e for e in amp.log if not e.startswith("q:")] == []


async def test_turn_on_cancelled_during_the_wake_still_cleans_up(
    hass: HomeAssistant,
) -> None:
    """Once power-on has gone out the revived zones must still be switched off."""
    amp = FakeAmp(master=False, boot_polls=5, standby_accepts_writes=False)
    amp.group_power = {0: False, 1: False, 2: True, 3: False}
    c = make(hass, amp)

    task = asyncio.ensure_future(c.async_turn_on(0))
    async with asyncio.timeout(2):
        while "amp:on" not in amp.log:
            await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await wait_for_unlock(c)

    assert amp.master is True
    assert "group0:on" not in amp.log
    assert [g for g in range(4) if amp.live(g)] == []


async def test_unreadable_mute_restores_the_last_polled_value(
    hass: HomeAssistant,
) -> None:
    amp = FakeAmp()
    amp.group_power[0] = False
    amp.mute[0] = True
    c = make(hass, amp)
    amp.unanswered_mute = 1

    await c.async_turn_on(0)
    amp.settle()

    assert amp.mute[0] is True


async def test_unreadable_mute_with_an_unmuted_last_value_stays_unmuted(
    hass: HomeAssistant,
) -> None:
    """The fallback is the last value, not always-mute, when there is one."""
    amp = FakeAmp()
    amp.group_power[0] = False
    c = make(hass, amp)
    amp.unanswered_mute = 1

    await c.async_turn_on(0)
    amp.settle()

    assert amp.mute[0] is False
    assert "mute0:on" not in amp.log


async def test_unreadable_and_unknown_mute_switches_on_muted(
    hass: HomeAssistant,
) -> None:
    """Silent is one tap to fix; playing where it should not is the failure."""
    amp = FakeAmp()
    amp.group_power[0] = False
    c = make(hass, amp)
    c.data.groups[0] = replace(c.data.groups[0], muted=None)
    amp.unanswered_mute = 1

    await c.async_turn_on(0)
    amp.settle()

    assert amp.mute[0] is True
    assert c.data.groups[0].muted is True


# ---------------------------------------------------------------------------
# Zones that are already on, and zones a wake would bring back
# ---------------------------------------------------------------------------


async def test_turn_on_leaves_a_zone_that_is_already_on_alone(
    hass: HomeAssistant,
) -> None:
    """Scenes call turn_on without checking; a playing zone must not be reset."""
    amp = FakeAmp()
    amp.volume[0] = -35
    amp.mute[0] = True
    c = make(hass, amp)

    await c.async_turn_on(0)
    amp.settle()

    assert "group0:on" not in amp.log
    assert amp.volume[0] == -35
    assert amp.mute[0] is True
    assert c.data.groups[0].volume_db == -35


async def test_already_on_is_judged_from_the_last_known_power_without_http(
    hass: HomeAssistant,
) -> None:
    amp = FakeAmp()
    amp.volume[0] = -35
    amp.http_up = False
    c = make(hass, amp)

    await c.async_turn_on(0)

    assert "group0:on" not in amp.log
    assert amp.volume[0] == -35


async def test_one_unanswered_amp_power_query_is_asked_again(
    hass: HomeAssistant,
) -> None:
    """Otherwise a single late reply sends a zone-on to a zone already playing."""
    amp = FakeAmp()
    amp.volume[0] = -35
    amp.unanswered_amp_power = 1
    c = make(hass, amp)

    await c.async_turn_on(0)

    assert "group0:on" not in amp.log
    assert amp.volume[0] == -35


async def test_turn_on_without_the_status_page_trusts_the_echo(
    hass: HomeAssistant,
) -> None:
    amp = FakeAmp()
    amp.group_power[0] = False
    amp.http_up = False
    c = make(hass, amp)
    c.data.group_power[0] = False

    await c.async_turn_on(0)

    assert amp.log.count("group0:on") == 1
    assert c.data.group_power[0] is True


async def test_waking_brings_back_only_the_zone_asked_for(
    hass: HomeAssistant,
) -> None:
    """Flags left on by a standby from outside HA must not ride the wake."""
    amp = FakeAmp(master=False)
    amp.group_power = {0: True, 1: False, 2: True, 3: True}
    c = make(hass, amp)
    c.data.amp_power = False

    await c.async_turn_on(1)

    assert amp.master is True
    assert [g for g in range(4) if amp.live(g)] == [1]
    assert amp.dropped == []
    assert c.data.group_power == {0: False, 1: True, 2: False, 3: False}
    # Switched off BEFORE the wake, so nothing played at all.
    wake = amp.log.index("amp:on")
    assert all(amp.log.index(f"group{g}:off") < wake for g in (0, 2, 3))


async def test_waking_brings_back_only_the_zone_asked_for_if_standby_ignores_it(
    hass: HomeAssistant,
) -> None:
    """Whether standby accepts a zone-off is unmeasured, so both are covered."""
    amp = FakeAmp(master=False, standby_accepts_writes=False)
    amp.group_power = {0: True, 1: False, 2: True, 3: True}
    c = make(hass, amp)

    await c.async_turn_on(1)

    assert [g for g in range(4) if amp.live(g)] == [1]
    assert c.data.group_power == {0: False, 1: True, 2: False, 3: False}


async def test_waking_survives_standby_not_answering_zone_commands(
    hass: HomeAssistant,
) -> None:
    """A zone-off that standby will not even echo must not fail the turn-on."""
    amp = FakeAmp(master=False, standby_echoes_writes=False)
    amp.group_power = {0: True, 1: False, 2: True, 3: True}
    c = make(hass, amp)

    await c.async_turn_on(1)

    assert [g for g in range(4) if amp.live(g)] == [1]


async def test_waking_leaves_zones_that_are_already_off_alone(
    hass: HomeAssistant,
) -> None:
    amp = FakeAmp(master=False)
    amp.group_power = {0: True, 1: False, 2: False, 3: False}
    c = make(hass, amp)

    await c.async_turn_on(1)

    assert "group0:off" in amp.log
    assert not any(f"group{g}:off" in amp.log for g in (2, 3))


async def test_unknown_amp_power_switches_no_other_zone_off(
    hass: HomeAssistant,
) -> None:
    """Not known to be in standby means other zones may be playing."""
    amp = FakeAmp()
    amp.group_power[1] = False
    amp.unanswered_amp_power = 2
    c = make(hass, amp)

    await c.async_turn_on(1)

    assert all(amp.group_power[g] for g in (0, 2, 3))
    assert not any(f"group{g}:off" in amp.log for g in (0, 2, 3))
    assert amp.group_power[1] is True


# ---------------------------------------------------------------------------
# Wakes that fail
# ---------------------------------------------------------------------------


async def test_a_failed_wake_is_not_retried_for_every_zone(
    hass: HomeAssistant,
) -> None:
    amp = FakeAmp(master=False, boot_polls=10**9)
    for g in amp.group_power:
        amp.group_power[g] = False
    c = make(hass, amp)

    with pytest.raises(HomeAssistantError):
        await c.async_turn_on(0)
    with pytest.raises(HomeAssistantError) as err:
        await c.async_turn_on(1)

    assert err.value.translation_key == "amp_wake_recently_failed"
    assert amp.log.count("amp:on") == 1


async def test_a_failed_wake_is_retried_once_the_amp_reports_on(
    hass: HomeAssistant,
) -> None:
    amp = FakeAmp(master=False, boot_polls=10**9)
    for g in amp.group_power:
        amp.group_power[g] = False
    c = make(hass, amp)
    with pytest.raises(HomeAssistantError):
        await c.async_turn_on(0)

    # Someone fixes it and wakes it by hand; a poll sees it On. Then it goes
    # back to standby, and the next turn-on must wake it rather than refuse.
    amp._booting = 0
    amp.master = True
    c.data = await c._async_update_data()
    amp.master = False
    amp.boot_polls = 2
    await c.async_turn_on(0)

    assert amp.log.count("amp:on") == 2
    assert amp.group_power[0] is True


async def test_a_failed_wake_is_retried_after_the_holdoff(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    amp = FakeAmp(master=False, boot_polls=10**9)
    for g in amp.group_power:
        amp.group_power[g] = False
    c = make(hass, amp)
    with pytest.raises(HomeAssistantError):
        await c.async_turn_on(0)

    monkeypatch.setattr(coord_mod, "WAKE_RETRY_HOLDOFF", 0)
    amp.boot_polls = 2
    amp._booting = 0
    await c.async_turn_on(0)

    assert amp.log.count("amp:on") == 2
    assert amp.group_power[0] is True


# ---------------------------------------------------------------------------
# Writes that arrive while a zone is being switched on
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("call", "expect"),
    [
        (lambda z: z.async_set_volume_level(0.5), "vol0:-35"),
        (lambda z: z.async_mute_volume(True), "mute0:on"),
        (lambda z: z.async_select_source("In2 L"), "src0:2"),
    ],
    ids=["set_volume_level", "mute", "select_source"],
)
async def test_every_write_during_a_wake_waits_for_it(
    hass: HomeAssistant, call, expect: str
) -> None:
    amp = FakeAmp(master=False, boot_polls=5)
    for g in amp.group_power:
        amp.group_power[g] = False
    c = make(hass, amp)
    z = zone(c, hass)

    turn_on = asyncio.ensure_future(c.async_turn_on(0))
    await wait_for_lock(c)
    await call(z)
    await turn_on

    assert amp.dropped == []
    assert amp.log.index(expect) > amp.log.index("group0:on")


async def test_a_direct_volume_step_during_a_wake_waits_for_it(
    hass: HomeAssistant,
) -> None:
    """Assist's relative volume calls the entity directly, not via a service.

    So PARALLEL_UPDATES does not hold it back, and without the coordinator's
    own lock the step reaches the booting amp and is dropped -- and would have
    been computed from the volume before the turn-on anyway.
    """
    amp = FakeAmp(master=False, boot_polls=5)
    for g in amp.group_power:
        amp.group_power[g] = False
    amp.volume[0] = -40
    c = make(hass, amp)
    c.data.groups[0] = replace(c.data.groups[0], volume_db=-40)
    z = zone(c, hass)

    turn_on = asyncio.ensure_future(c.async_turn_on(0))
    await wait_for_lock(c)
    await z.async_volume_up()
    await turn_on

    assert amp.dropped == []
    assert amp.volume[0] == -40 + 1


async def test_a_change_during_a_poll_is_not_overwritten(hass: HomeAssistant) -> None:
    """A poll that read before a change landed must not publish over it."""
    amp = FakeAmp()
    c = make(hass, amp)
    real = amp.read_group

    async def read_group(group: int) -> GroupState:
        state = await real(group)
        if group == 0:
            c.apply_optimistic(0, volume_db=-10)
        return state

    c.client.read_group = read_group
    data = await c._async_update_data()

    assert data.groups[0].volume_db == -10


async def test_a_turn_on_during_a_poll_is_not_overwritten(
    hass: HomeAssistant,
) -> None:
    """The poll read the zone as off and at its old volume before it switched on."""
    amp = FakeAmp()
    amp.group_power[0] = False
    amp.volume[0] = -40
    c = make(hass, amp)
    c.data.group_power[0] = False
    real = amp.read_group

    async def read_group(group: int) -> GroupState:
        state = await real(group)
        if group == 1:
            await c.async_turn_on(0, ceiling_db=-45)
        return state

    c.client.read_group = read_group
    data = await c._async_update_data()

    # -45 is the restore (capped); the poll read -40 before the turn-on.
    assert data.groups[0].volume_db == -45
    assert data.group_power[0] is True


async def test_no_poll_while_a_command_holds_the_lock(hass: HomeAssistant) -> None:
    amp = FakeAmp()
    c = make(hass, amp)

    async with c.command_lock:
        data = await c._async_update_data()

    assert data is c.data
    assert amp.log == []


# ---------------------------------------------------------------------------
# Turning off, when the cache is wrong or the amp is asleep
# ---------------------------------------------------------------------------


async def test_last_zone_off_sends_standby_even_if_the_cache_says_standby(
    hass: HomeAssistant,
) -> None:
    amp = FakeAmp()
    for g in (1, 2, 3):
        amp.group_power[g] = False
    c = make(hass, amp)
    c.data.amp_power = False  # stale

    await c.async_turn_off(0)

    assert "amp:off" in amp.log
    assert amp.master is False


async def test_turn_off_in_standby_survives_no_answer(hass: HomeAssistant) -> None:
    amp = FakeAmp(master=False, standby_echoes_writes=False)
    c = make(hass, amp)

    await c.async_turn_off(0)

    assert zone(c, hass).state is MediaPlayerState.OFF


async def test_turn_off_in_standby_is_best_effort(hass: HomeAssistant) -> None:
    """A zone in a sleeping amp is already silent; failing would help nobody."""
    amp = FakeAmp(master=False, standby_accepts_writes=False)
    c = make(hass, amp)

    await c.async_turn_off(0)

    # Shown as the status page has it -- the flag did not clear -- while the
    # zone itself is off because the amp is.
    assert c.data.group_power[0] is True
    assert "amp:off" not in amp.log
    assert zone(c, hass).state is MediaPlayerState.OFF


async def test_turn_off_in_standby_shows_the_flag_cleared_when_it_did(
    hass: HomeAssistant,
) -> None:
    amp = FakeAmp(master=False)
    c = make(hass, amp)

    await c.async_turn_off(0)

    assert amp.group_power[0] is False
    assert c.data.group_power[0] is False


# ---------------------------------------------------------------------------
# Polls that miss something
# ---------------------------------------------------------------------------


async def test_a_status_page_outage_keeps_the_last_zone_power(
    hass: HomeAssistant,
) -> None:
    """Unknown shows as off in HomeKit, and toggle answers unknown with OFF."""
    amp = FakeAmp()
    c = make(hass, amp)
    c.data = await c._async_update_data()
    amp.http_up = False

    c.data = await c._async_update_data()

    assert c.data.group_power == {0: True, 1: True, 2: True, 3: True}
    assert zone(c, hass).state is MediaPlayerState.ON


async def test_a_long_status_page_outage_forgets_zone_power(
    hass: HomeAssistant,
) -> None:
    amp = FakeAmp()
    c = make(hass, amp)
    c.data = await c._async_update_data()
    amp.http_up = False
    c._group_power_at -= coord_mod.GROUP_POWER_STALE_AFTER + 1

    c.data = await c._async_update_data()

    assert c.data.group_power == {}


async def test_the_outage_bound_stretches_to_three_long_poll_intervals(
    hass: HomeAssistant,
) -> None:
    """At a 300 s interval a 300 s bound would not survive a single miss."""
    amp = FakeAmp()
    c = make(hass, amp)
    c.update_interval = timedelta(seconds=300)
    c.data = await c._async_update_data()
    amp.http_up = False
    c._group_power_at -= 2 * 300

    c.data = await c._async_update_data()

    assert c.data.group_power == {0: True, 1: True, 2: True, 3: True}


async def test_a_missed_amp_power_reply_keeps_on(hass: HomeAssistant) -> None:
    """One late reply should not flicker every playing zone to unknown."""
    amp = FakeAmp()
    c = make(hass, amp)
    amp.unanswered_amp_power = 1

    c.data = await c._async_update_data()

    assert c.data.amp_power is True
    assert zone(c, hass).state is MediaPlayerState.ON


async def test_a_missed_amp_power_reply_does_not_keep_standby(
    hass: HomeAssistant,
) -> None:
    """If it was woken from outside HA meanwhile, "standby" would show OFF over audio.

    Unknown is the honest answer, and a flagged zone then shows unknown.
    """
    amp = FakeAmp(master=False)
    c = make(hass, amp)
    amp.unanswered_amp_power = 1

    c.data = await c._async_update_data()

    assert c.data.amp_power is None
    assert zone(c, hass).state is None


async def test_only_a_few_missed_amp_power_replies_are_bridged(
    hass: HomeAssistant,
) -> None:
    amp = FakeAmp()
    c = make(hass, amp)
    amp.unanswered_amp_power = 10

    for _ in range(coord_mod.AMP_POWER_MISSES_BRIDGED + 1):
        c.data = await c._async_update_data()

    assert c.data.amp_power is None


async def test_an_answer_from_a_command_resets_the_miss_count(
    hass: HomeAssistant,
) -> None:
    amp = FakeAmp()
    amp.group_power[1] = False
    c = make(hass, amp)
    amp.unanswered_amp_power = coord_mod.AMP_POWER_MISSES_BRIDGED
    for _ in range(coord_mod.AMP_POWER_MISSES_BRIDGED):
        c.data = await c._async_update_data()  # misses, bridged, now at the limit

    await c.async_turn_on(1)  # answers
    amp.unanswered_amp_power = 1
    c.data = await c._async_update_data()

    assert c.data.amp_power is True


async def test_zone_is_unknown_when_amp_power_is_unknown_and_its_flag_is_on(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set("media_player.streamer", "playing", {"media_title": "X"})
    amp = FakeAmp()
    c = make(hass, amp)
    c.data.amp_power = None
    z = zone(c, hass)
    z._resubscribe()

    assert z.state is None
    assert z.media_title is None
    c.data.group_power[0] = False
    assert z.state is MediaPlayerState.OFF


# ---------------------------------------------------------------------------
# Round two: the mute restore under an unmeasured clear, and failures after
# the zone-on
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("master", [True, False], ids=["awake", "from_standby"])
async def test_restore_survives_a_clear_that_lands_on_top_of_it(
    hass: HomeAssistant, master: bool
) -> None:
    """Whether a mute sent straight after a zone-on beats the clear is unmeasured.

    In this model it does not: the clear lands afterwards and wins. The
    restore is read back across the window and sent again, so the zone still
    ends up muted.
    """
    amp = FakeAmp(master=master, clear_overrides_mute=True)
    amp.group_power[0] = False
    amp.mute[0] = True
    c = make(hass, amp)

    await c.async_turn_on(0)
    amp.settle()

    assert amp.mute[0] is True
    assert amp.log.count("mute0:on") >= 2
    assert c.data.groups[0].muted is True
    # Re-sent at the read that found it lost, so the later reads confirm it
    # and no extra check is needed at the end. The window is longer after a
    # wake.
    window = (
        coord_mod.MUTE_VERIFY_DELAYS
        if master
        else coord_mod.MUTE_VERIFY_DELAYS_AFTER_WAKE
    )
    assert amp.log.count("q:mute0") == 1 + len(window)


async def test_an_unmuted_zone_is_not_verified(hass: HomeAssistant) -> None:
    amp = FakeAmp()
    amp.group_power[0] = False
    c = make(hass, amp)

    await c.async_turn_on(0)

    assert amp.log.count("q:mute0") == 1


@pytest.mark.parametrize("overrides", [False, True], ids=["clear_cancelled", "late"])
async def test_a_zone_on_applied_but_never_echoed_still_ends_muted(
    hass: HomeAssistant, overrides: bool
) -> None:
    """A late echo does not mean the zone-on was not applied.

    With a clear that lands late, the fail-safe's own mute must be verified too.
    """
    amp = FakeAmp(clear_overrides_mute=overrides)
    amp.group_power[0] = False
    amp.mute[0] = True
    amp.lose_echo_next("group0:on")
    c = make(hass, amp)

    with pytest.raises(SonanceConnectionError):
        await c.async_turn_on(0)
    amp.settle()

    assert amp.group_power[0] is True
    assert amp.mute[0] is True
    # Shown as the status page has it -- on -- not left showing OFF.
    assert c.data.group_power[0] is True
    assert c.data.groups[0].muted is True


async def test_a_mute_restore_never_echoed_is_sent_again(hass: HomeAssistant) -> None:
    amp = FakeAmp()
    amp.group_power[0] = False
    amp.mute[0] = True
    amp.lose_echo_next("mute0:on")
    c = make(hass, amp)

    with pytest.raises(SonanceConnectionError):
        await c.async_turn_on(0)
    amp.settle()

    assert amp.mute[0] is True


async def test_a_zone_that_cannot_be_muted_is_switched_off(
    hass: HomeAssistant,
) -> None:
    """Silent is one tap to fix; playing where it was muted is the failure."""
    amp = FakeAmp()
    amp.group_power[0] = False
    amp.mute[0] = True
    amp.fail_next("mute0:on", times=3)
    c = make(hass, amp)

    with pytest.raises(SonanceConnectionError):
        await c.async_turn_on(0)
    amp.settle()

    assert amp.live(0) is False
    assert "group0:off" in amp.log


async def test_turn_on_that_never_shows_raises_and_publishes_nothing(
    hass: HomeAssistant,
) -> None:
    amp = FakeAmp()
    amp.group_power[0] = False
    amp.mute[0] = True
    c = make(hass, amp)

    async def mute_only(group: int) -> None:
        amp.log.append(f"group{group}:on")
        await amp.set_mute(group, True)

    c.client.set_group_power_muted = mute_only

    with pytest.raises(HomeAssistantError) as err:
        await c.async_turn_on(0)

    assert err.value.translation_key == "zone_power_not_confirmed"
    sent = [e for e in amp.log if e in ("group0:on", "mute0:on")]
    assert sent[:4] == ["group0:on", "mute0:on", "group0:on", "mute0:on"]
    assert c.data.group_power[0] is False


# ---------------------------------------------------------------------------
# Round two: waking, when parts of it fail
# ---------------------------------------------------------------------------


async def test_amp_power_unknown_switches_nothing(hass: HomeAssistant) -> None:
    """A guess is how zones nobody asked for start playing."""
    amp = FakeAmp()
    amp.group_power[1] = False
    amp.unanswered_amp_power = 10
    c = make(hass, amp)

    with pytest.raises(HomeAssistantError) as err:
        await c.async_turn_on(1)

    assert err.value.translation_key == "amp_power_unknown"
    assert set(amp.log) == {"q:master"}


async def test_a_zone_whose_flag_survived_standby_gets_its_turn_on_volume(
    hass: HomeAssistant,
) -> None:
    """Switched off before the wake, so its zone-on is the measured off-to-on."""
    amp = FakeAmp(master=False)
    amp.group_power = {0: True, 1: False, 2: False, 3: False}
    amp.volume[0] = -5
    c = make(hass, amp)

    await c.async_turn_on(0)

    assert amp.log.index("group0:off") < amp.log.index("amp:on")
    assert amp.log.index("group0:on") > amp.log.index("amp:on")
    assert [g for g in range(4) if amp.live(g)] == [0]


@pytest.mark.parametrize("accepts", [True, False], ids=["standby_accepts", "ignores"])
async def test_waking_without_the_status_page_brings_back_only_one(
    hass: HomeAssistant, accepts: bool
) -> None:
    amp = FakeAmp(master=False, standby_accepts_writes=accepts)
    amp.group_power = {0: True, 1: False, 2: True, 3: True}
    c = make(hass, amp)
    amp.http_up = False

    await c.async_turn_on(1)

    assert [g for g in range(4) if amp.live(g)] == [1]
    # The zones switched off after the wake were each acknowledged, so they
    # are shown off -- not left showing the stale "on" from before.
    assert c.data.group_power == {0: False, 1: True, 2: False, 3: False}


async def test_a_revived_zone_that_will_not_switch_off_is_shown_on(
    hass: HomeAssistant,
) -> None:
    """Not OFF while it plays. The error is raised after publishing that."""
    amp = FakeAmp(master=False, standby_accepts_writes=False)
    amp.group_power = {0: True, 1: False, 2: True, 3: True}
    amp.fail_next("group2:off", times=2)
    c = make(hass, amp)

    with pytest.raises(SonanceConnectionError):
        await c.async_turn_on(1)

    assert amp.live(2) is True
    assert "group1:on" not in amp.log
    assert c.data.amp_power is True
    assert c.data.group_power[2] is True
    assert zone(c, hass, 2).state is MediaPlayerState.ON
    # The other revived zones were still switched off.
    assert not amp.live(0) and not amp.live(3)


async def test_a_revived_zone_that_fails_once_is_retried(
    hass: HomeAssistant,
) -> None:
    amp = FakeAmp(master=False, standby_accepts_writes=False)
    amp.group_power = {0: True, 1: False, 2: True, 3: True}
    amp.fail_next("group2:off", times=1)
    c = make(hass, amp)

    await c.async_turn_on(1)

    assert [g for g in range(4) if amp.live(g)] == [1]


async def test_the_wake_holdoff_is_checked_before_anything_is_sent(
    hass: HomeAssistant,
) -> None:
    amp = FakeAmp(master=False, boot_polls=10**9, standby_accepts_writes=False)
    amp.group_power = {0: False, 1: False, 2: True, 3: False}
    c = make(hass, amp)
    with pytest.raises(HomeAssistantError):
        await c.async_turn_on(0)
    before = len(amp.log)

    with pytest.raises(HomeAssistantError):
        await c.async_turn_on(1)

    assert set(amp.log[before:]) <= {"q:master"}


async def test_a_hung_status_page_is_waited_on_once_per_command(
    hass: HomeAssistant,
) -> None:
    amp = FakeAmp()
    amp.group_power[0] = False
    c = make(hass, amp)
    c.data.group_power[0] = False
    amp.http_hang = True

    await c.async_turn_on(0)

    assert amp.http_calls == 1
    assert amp.group_power[0] is True


# ---------------------------------------------------------------------------
# Round two: polls, unload, volume, and the entity's own wiring
# ---------------------------------------------------------------------------


async def test_a_skipped_poll_after_a_failed_one_is_still_a_failure(
    hass: HomeAssistant,
) -> None:
    """Otherwise it marks the zones available again without reading anything."""
    amp = FakeAmp()
    c = make(hass, amp)
    c.last_update_success = False

    async with c.command_lock:
        with pytest.raises(UpdateFailed):
            await c._async_update_data()


async def test_unload_waits_for_a_turn_on_in_progress(hass: HomeAssistant) -> None:
    """Cutting a wake off leaves the zones it revived playing."""
    amp = FakeAmp(master=False, boot_polls=5, standby_accepts_writes=False)
    amp.group_power = {0: False, 1: False, 2: True, 3: False}
    c = make(hass, amp)

    turn_on = asyncio.ensure_future(c.async_turn_on(0))
    await wait_for_lock(c)
    await c.async_close()
    await turn_on

    assert amp.log.index("disconnect") > amp.log.index("group0:on")
    assert [g for g in range(4) if amp.live(g)] == [0]
    with pytest.raises(HomeAssistantError) as err:
        await c.async_turn_on(1)
    assert err.value.translation_key == "not_connected"


async def test_turn_on_asks_twice_for_the_volume(hass: HomeAssistant) -> None:
    """On the already-on path, the only read is the refresh; one late reply."""
    amp = FakeAmp()
    c = make(hass, amp)
    c.data.groups[3] = replace(c.data.groups[3], volume_db=-30)  # stale
    amp.unanswered_volume = 1

    await c.async_turn_on(3)

    assert c.data.groups[3].volume_db == -49
    assert c.volume_verified(3)


async def test_a_volume_step_starts_from_the_amp_not_the_cache(
    hass: HomeAssistant,
) -> None:
    """A stale cached level would turn a one-dB step into a jump."""
    amp = FakeAmp()
    c = make(hass, amp)
    c.data.groups[0] = replace(c.data.groups[0], volume_db=-10)
    z = zone(c, hass)

    await z.async_volume_up()

    assert amp.volume[0] == FakeAmp.TURN_ON_VOLUME + 1
    assert c.data.groups[0].volume_db == FakeAmp.TURN_ON_VOLUME + 1


async def test_a_volume_step_without_a_reading_uses_the_amps_own_step(
    hass: HomeAssistant,
) -> None:
    amp = FakeAmp()
    c = make(hass, amp)
    c.client.volume_up = MagicMock(side_effect=lambda g: asyncio.sleep(0))
    amp.unanswered_volume = 1
    z = zone(c, hass)

    await z.async_volume_up()

    c.client.volume_up.assert_called_once_with(0)


async def test_an_off_zone_reports_no_volume_level(hass: HomeAssistant) -> None:
    """Assist's percentage step reads it directly, and would jump from it."""
    amp = FakeAmp()
    c = make(hass, amp)
    c.data.group_power[0] = False

    assert zone(c, hass).volume_level is None


async def test_the_entity_switches_its_own_zone(hass: HomeAssistant) -> None:
    amp = FakeAmp()
    amp.group_power[2] = False
    c = make(hass, amp)
    z = zone(c, hass, 2)

    await z.async_turn_on()
    assert "group2:on" in amp.log
    await z.async_turn_off()
    assert "group2:off" in amp.log

    assert not any(e.startswith(("group0", "group1", "group3")) for e in amp.log)


async def test_turn_off_with_one_unanswered_amp_query_on_an_awake_amp(
    hass: HomeAssistant,
) -> None:
    """Unknown must not be read as standby: that would show playing zones OFF."""
    amp = FakeAmp()
    amp.unanswered_amp_power = 1
    c = make(hass, amp)

    await c.async_turn_off(0)

    assert [g for g in range(4) if amp.live(g)] == [1, 2, 3]
    assert c.data.amp_power is True
    assert zone(c, hass, 1).state is MediaPlayerState.ON


async def test_a_mute_lost_at_the_last_check_is_checked_once_more(
    hass: HomeAssistant,
) -> None:
    amp = FakeAmp(clear_overrides_mute=True)
    amp.MUTE_CLEAR_LAG = 5  # lands on the fourth and last read-back
    amp.group_power[0] = False
    amp.mute[0] = True
    c = make(hass, amp)

    await c.async_turn_on(0)
    amp.settle()

    assert amp.mute[0] is True
    assert amp.log.count("q:mute0") == 1 + len(coord_mod.MUTE_VERIFY_DELAYS) + 1


async def test_a_zone_that_can_be_neither_muted_nor_switched_off_is_reported(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    amp = FakeAmp()
    amp.group_power[0] = False
    amp.mute[0] = True
    amp.fail_next("mute0:on", times=3)
    amp.fail_next("group0:off")
    c = make(hass, amp)

    with pytest.raises(SonanceConnectionError):
        await c.async_turn_on(0)

    assert "may be playing unmuted" in caplog.text
    # On, per the status page, and its mute unknown -- not shown as muted.
    assert c.data.group_power[0] is True
    assert c.data.groups[0].muted is None


async def test_a_revived_zone_that_will_not_switch_off_without_http_is_unknown(
    hass: HomeAssistant,
) -> None:
    amp = FakeAmp(master=False, standby_accepts_writes=False)
    amp.group_power = {0: True, 1: False, 2: True, 3: True}
    amp.fail_next("group2:off", times=2)
    c = make(hass, amp)
    amp.http_up = False

    with pytest.raises(SonanceConnectionError):
        await c.async_turn_on(1)

    assert 2 not in c.data.group_power
    assert c.data.group_power[0] is False
    assert c.data.group_power[3] is False


async def test_turn_off_in_standby_with_one_unanswered_amp_query(
    hass: HomeAssistant,
) -> None:
    """Asked again, so the tolerant standby branch is taken rather than a confirm
    that cannot succeed if standby ignores the zone-off."""
    amp = FakeAmp(master=False, standby_accepts_writes=False)
    amp.unanswered_amp_power = 1
    c = make(hass, amp)

    await c.async_turn_off(0)

    assert zone(c, hass).state is MediaPlayerState.OFF


async def test_a_poll_across_a_failed_wake_does_not_republish_standby(
    hass: HomeAssistant,
) -> None:
    """A revived zone left playing must not go back to showing OFF."""
    amp = FakeAmp(master=False, standby_accepts_writes=False)
    amp.group_power = {0: True, 1: False, 2: True, 3: True}
    amp.fail_next("group2:off", times=2)
    c = make(hass, amp)
    real = c.http.group_power
    fired = False

    async def group_power() -> dict[int, bool]:
        nonlocal fired
        if not fired:
            # The poll has read the amp as in standby; the turn-on lands now.
            fired = True
            with pytest.raises(SonanceConnectionError):
                await c.async_turn_on(1)
        return await real()

    c.http.group_power = group_power
    c.data = await c._async_update_data()

    assert amp.live(2) is True
    assert zone(c, hass, 2).state is MediaPlayerState.ON



# ---------------------------------------------------------------------------
# Round three
# ---------------------------------------------------------------------------


async def test_a_failed_volume_read_back_still_publishes_the_zone_on(
    hass: HomeAssistant,
) -> None:
    """The switch-on worked; a read-back that raises must not hide that."""
    amp = FakeAmp()
    amp.group_power[0] = False
    c = make(hass, amp)
    c.data.group_power[0] = False

    async def broken(group: int) -> int:
        raise SonanceConnectionError("no reply")

    c.client.get_volume = broken
    await c.async_turn_on(0)

    assert zone(c, hass).state is MediaPlayerState.ON


async def test_an_unread_volume_is_not_offered_as_a_level_until_read(
    hass: HomeAssistant,
) -> None:
    """Assist's percentage step would jump from the pre-switch-on level."""
    amp = FakeAmp()
    amp.group_power[0] = False
    c = make(hass, amp)
    c.data.group_power[0] = False
    c.data.groups[0] = replace(c.data.groups[0], volume_db=-3)
    amp.unanswered_volume = 20
    z = zone(c, hass)

    await c.async_turn_on(0)
    assert z.state is MediaPlayerState.ON
    assert z.volume_level is None

    amp.unanswered_volume = 0
    c.data = await c._async_update_data()
    assert z.volume_level == pytest.approx((amp.volume[0] + 70) / 70)


async def test_turn_off_in_standby_without_the_page_leaves_the_flag_unknown(
    hass: HomeAssistant,
) -> None:
    """An echo in standby is not proof the flag cleared."""
    amp = FakeAmp(master=False, standby_accepts_writes=False)
    c = make(hass, amp)
    amp.http_up = False

    await c.async_turn_off(0)

    assert 0 not in c.data.group_power
    assert c.data.amp_power is False


@pytest.mark.parametrize("muted", [False, True], ids=["playing", "muted"])
async def test_a_zone_whose_power_is_unknown_is_switched_on_harmlessly(
    hass: HomeAssistant, muted: bool
) -> None:
    """A zone-on to a zone already on does nothing (measured), so no refusal."""
    amp = FakeAmp()
    amp.volume[0] = -35
    amp.mute[0] = muted
    c = make(hass, amp)
    c.data.group_power = {}
    amp.http_up = False

    await c.async_turn_on(0, ceiling_db=0)
    amp.settle()

    assert "group0:on" in amp.log
    assert amp.volume[0] == -35
    assert amp.mute[0] is muted
    assert c.data.group_power[0] is True


# ---------------------------------------------------------------------------
# Held levels and owed mutes survive a restart
# ---------------------------------------------------------------------------


async def _flush_store(hass: HomeAssistant) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=5))
    await hass.async_block_till_done()


async def test_a_hold_survives_a_restart(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    amp = FakeAmp()
    amp.turn_on_volume = -70
    amp.group_power[0] = False
    amp.volume[0] = -35
    amp.mute[0] = True
    c = make(hass, amp)
    await c.async_turn_on(0, ceiling_db=0)
    await _flush_store(hass)

    again = make(hass, amp, entry=c.config_entry)
    await again.async_load_holds()

    assert again.held_level(0) == -35


async def test_an_owed_mute_survives_a_restart(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    amp = FakeAmp()
    c = make(hass, amp)
    c._owe_mute(2)
    await _flush_store(hass)

    again = make(hass, amp, entry=c.config_entry)
    await again.async_load_holds()

    assert again._mute_to_restore(2, False) is True


async def test_saved_state_for_unknown_zones_or_levels_is_dropped(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    amp = FakeAmp()
    c = make(hass, amp)
    key = holds_store_key(c.config_entry.entry_id)
    hass_storage[key] = {
        "version": 1,
        "minor_version": 1,
        "key": key,
        "data": {
            "held_level": {"0": -35, "7": -20, "1": 99},
            "mute_owed": [2, 6],
        },
    }

    await c.async_load_holds()

    assert c.held_level(0) == -35
    assert c.held_level(1) is None  # out of range
    assert c.held_level(7) is None  # not a zone here
    assert c._mute_owed == {2}


async def test_unreadable_saved_state_is_ignored(
    hass: HomeAssistant, hass_storage: dict, caplog: pytest.LogCaptureFixture
) -> None:
    amp = FakeAmp()
    c = make(hass, amp)
    key = holds_store_key(c.config_entry.entry_id)
    hass_storage[key] = {
        "version": 1,
        "minor_version": 1,
        "key": key,
        "data": {"held_level": {"A": "loud"}},
    }

    await c.async_load_holds()

    assert c.held_level(0) is None
    assert "Ignoring unreadable saved zone state" in caplog.text


async def test_removing_the_entry_deletes_its_saved_state(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    amp = FakeAmp()
    c = make(hass, amp)
    c._set_hold(0, -35)
    await _flush_store(hass)
    key = holds_store_key(c.config_entry.entry_id)
    assert key in hass_storage

    await async_remove_holds(hass, c.config_entry)

    assert key not in hass_storage


async def test_a_mute_owed_from_a_failed_switch_on_is_restored_next_time(
    hass: HomeAssistant,
) -> None:
    """The failed switch-on cleared the mute on the amp; it must come back muted."""
    amp = FakeAmp()
    amp.group_power[0] = False
    amp.mute[0] = True
    amp.fail_next("mute0:on", times=20)
    c = make(hass, amp)
    with pytest.raises(SonanceConnectionError):
        await c.async_turn_on(0)
    amp.settle()
    assert amp.live(0) is False
    amp._fail.clear()
    amp.mute[0] = False  # the amp lost it

    await c.async_turn_on(0)
    amp.settle()

    assert amp.mute[0] is True


async def test_a_user_mute_cancels_an_owed_mute(hass: HomeAssistant) -> None:
    amp = FakeAmp()
    amp.group_power[0] = False
    c = make(hass, amp)
    c._mute_owed.add(0)
    z = zone(c, hass)

    await z.async_mute_volume(False)
    await c.async_turn_on(0)
    amp.settle()

    assert amp.mute[0] is False


async def test_an_unanswered_verify_read_is_not_a_confirmation(
    hass: HomeAssistant,
) -> None:
    """Silence is not "muted": the mute is re-sent until a read confirms it."""
    amp = FakeAmp(clear_overrides_mute=True)
    amp.group_power[0] = False
    amp.mute[0] = True
    c = make(hass, amp)
    real = amp.get_mute
    reads = {"n": 0}

    async def get_mute(group: int) -> bool | None:
        value = await real(group)
        reads["n"] += 1
        # The pre-read answers; every verify read after the clear lands does not.
        return value if reads["n"] < 3 else None

    c.client.get_mute = get_mute

    with pytest.raises(SonanceConnectionError):
        await c.async_turn_on(0)
    amp.settle()

    # Never confirmed, so it went the fail-safe way -- and stayed silent.
    assert amp.mute[0] is True or amp.live(0) is False


async def test_turn_off_retries_a_zone_off_that_got_no_answer(
    hass: HomeAssistant,
) -> None:
    amp = FakeAmp()
    amp.fail_next("group0:off")
    c = make(hass, amp)

    await c.async_turn_off(0)

    assert amp.group_power[0] is False
    assert amp.log.count("group0:off") == 2


async def test_turn_off_trusts_the_page_when_the_echo_was_lost(
    hass: HomeAssistant,
) -> None:
    amp = FakeAmp()
    amp.lose_echo_next("group0:off")
    c = make(hass, amp)

    await c.async_turn_off(0)

    assert amp.log.count("group0:off") == 1
    assert c.data.group_power[0] is False


async def test_cancelled_during_the_pre_clear_does_not_wake(
    hass: HomeAssistant,
) -> None:
    amp = FakeAmp(master=False)
    amp.group_power = {0: True, 1: False, 2: False, 3: False}
    c = make(hass, amp)
    real = amp.set_group_power
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow(group: int, on: bool) -> None:
        started.set()
        await release.wait()
        await real(group, on)

    c.client.set_group_power = slow
    task = asyncio.ensure_future(c.async_turn_on(1))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    await wait_for_unlock(c)

    assert "amp:on" not in amp.log
    assert c.data.amp_power is False


async def test_a_power_on_whose_echo_is_lost_still_cleans_up_after_the_wake(
    hass: HomeAssistant,
) -> None:
    amp = FakeAmp(master=False, standby_accepts_writes=False)
    amp.group_power = {0: True, 1: False, 2: True, 3: True}
    amp.lose_echo_next("amp:on")
    c = make(hass, amp)

    await c.async_turn_on(1)

    assert [g for g in range(4) if amp.live(g)] == [1]


async def test_zones_are_switched_off_after_the_wake_even_if_standby_said_cleared(
    hass: HomeAssistant,
) -> None:
    """Nothing seen in standby -- echo or page -- proves the wake will not revive it."""
    amp = FakeAmp(master=False)
    amp.group_power = {0: True, 1: False, 2: True, 3: False}
    amp.revive_on_wake = {0, 2}
    c = make(hass, amp)

    await c.async_turn_on(1)

    assert [g for g in range(4) if amp.live(g)] == [1]


async def test_the_pre_clear_stops_at_the_first_unanswered_zone(
    hass: HomeAssistant,
) -> None:
    """Standby evidently not answering: do not wait out a timeout per zone."""
    amp = FakeAmp(master=False, standby_echoes_writes=False)
    amp.group_power = {0: True, 1: False, 2: True, 3: True}
    c = make(hass, amp)

    await c.async_turn_on(1)

    wake = amp.log.index("amp:on")
    assert [e for e in amp.log[:wake] if e.endswith(":off")] == ["group0:off"]
    assert [g for g in range(4) if amp.live(g)] == [1]


async def test_a_zone_on_that_never_left_changes_nothing(hass: HomeAssistant) -> None:
    """No connection, so no zone-on -- and no fail-safe for a zone that is off."""
    amp = FakeAmp()
    amp.group_power[0] = False
    amp.mute[0] = True
    amp.not_sent_next("group0:on")
    c = make(hass, amp)
    c.data.group_power[0] = False

    with pytest.raises(SonanceNotSentError):
        await c.async_turn_on(0)

    assert "mute0:on" not in amp.log
    assert c.data.group_power[0] is False


async def test_a_poll_retries_once_before_calling_the_amp_unreachable(
    hass: HomeAssistant,
) -> None:
    """An unavailable entity ignores turn_off, so one blip must not cause it."""
    amp = FakeAmp()
    c = make(hass, amp)
    amp.read_group_failures = 1

    data = await c._async_update_data()

    assert data.groups[0].volume_db == amp.volume[0]


async def test_a_poll_that_fails_twice_asks_to_be_retried_soon(
    hass: HomeAssistant,
) -> None:
    amp = FakeAmp()
    c = make(hass, amp)
    amp.read_group_failures = 2

    with pytest.raises(UpdateFailed) as err:
        await c._async_update_data()

    assert err.value.retry_after == coord_mod.POLL_RETRY_AFTER


async def test_a_step_up_without_a_reading_respects_the_ceiling(
    hass: HomeAssistant,
) -> None:
    amp = FakeAmp()
    c = make(hass, amp)
    c.data.groups[0] = replace(c.data.groups[0], volume_db=0)
    c.client.volume_up = MagicMock(side_effect=lambda g: asyncio.sleep(0))
    amp.unanswered_volume = 1
    z = zone(c, hass)  # ceiling 0 dB

    await z.async_volume_up()

    c.client.volume_up.assert_not_called()


async def test_a_failure_after_the_caller_went_away_is_logged(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    amp = FakeAmp()
    amp.group_power[0] = False
    c = make(hass, amp)
    real = amp.set_group_power
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_then_fail(group: int, on: bool) -> None:
        await real(group, on)
        started.set()
        await release.wait()
        raise SonanceConnectionError("no reply")

    c.client.set_group_power = slow_then_fail
    task = asyncio.ensure_future(c.async_turn_on(0))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    await wait_for_unlock(c)
    await asyncio.sleep(0)

    assert "after the request was cancelled" in caplog.text


async def test_a_failed_switch_on_without_the_page_shows_the_zone_unknown(
    hass: HomeAssistant,
) -> None:
    """It may well be on: not OFF."""
    amp = FakeAmp()
    amp.group_power[0] = False
    amp.mute[0] = True
    amp.lose_echo_next("group0:on")
    c = make(hass, amp)
    c.data.group_power[0] = False
    amp.http_up = False

    with pytest.raises(SonanceConnectionError):
        await c.async_turn_on(0)

    assert 0 not in c.data.group_power
    assert c.data.groups[0].muted is True



# ---------------------------------------------------------------------------
# The volume after a zone-on
#
# Measured 2026-09-27: the amp applies the zone's turn-on volume ~0.2 s after a
# zone-on, over anything sent before, and holds the mute off until ~1.05 s --
# audibly. With the turn-on volume at -70 dB that window is silent, and the
# level the zone had is put back once it has passed.
# ---------------------------------------------------------------------------


async def test_a_muted_zone_switched_on_stays_silent_and_holds_its_level(
    hass: HomeAssistant,
) -> None:
    """The incident of 2026-09-27: a restore would have un-muted it."""
    amp = FakeAmp()
    amp.turn_on_volume = -70
    amp.group_power[0] = False
    amp.volume[0] = -35
    amp.mute[0] = True
    c = make(hass, amp)

    await c.async_turn_on(0, ceiling_db=0)
    amp.settle()

    assert amp.mute[0] is True
    assert amp.volume[0] == -70
    assert not any(e.startswith("vol0:") for e in amp.log)
    assert c.data.groups[0].volume_db == -35  # shown: the level it will have
    assert c.held_level(0) == -35


async def test_the_level_comes_from_the_amp_not_the_cache(hass: HomeAssistant) -> None:
    """A switched-off zone still reports its volume, so a restart loses nothing."""
    amp = FakeAmp()
    amp.turn_on_volume = -70
    amp.group_power[0] = False
    amp.volume[0] = -35
    c = make(hass, amp)
    c.data.groups[0] = replace(c.data.groups[0], volume_db=-10)

    await c.async_turn_on(0, ceiling_db=0)
    amp.settle()

    assert amp.volume[0] == -35


async def test_the_restored_level_never_exceeds_the_ceiling(
    hass: HomeAssistant,
) -> None:
    amp = FakeAmp()
    amp.turn_on_volume = -70
    amp.group_power[0] = False
    amp.volume[0] = -5
    c = make(hass, amp)

    await c.async_turn_on(0, ceiling_db=-20)
    amp.settle()

    assert amp.volume[0] == -20


async def test_the_entity_restores_up_to_its_own_ceiling(hass: HomeAssistant) -> None:
    amp = FakeAmp()
    amp.turn_on_volume = -70
    amp.group_power[0] = False
    amp.volume[0] = -5
    c = make(hass, amp)
    z = zone(c, hass, 0, max_db=-20)

    await z.async_turn_on()
    amp.settle()

    assert amp.volume[0] == -20


async def test_an_unknown_level_leaves_the_turn_on_volume(hass: HomeAssistant) -> None:
    """With nothing to restore to, the silent power-up level stays."""
    amp = FakeAmp()
    amp.turn_on_volume = -70
    amp.group_power[0] = False
    c = make(hass, amp)
    c.data.groups[0] = replace(c.data.groups[0], volume_db=None)
    real = amp.get_volume
    reads = {"n": 0}

    async def get_volume(group: int) -> int | None:
        reads["n"] += 1
        if reads["n"] <= 2:
            return None  # both reads before the zone-on go unanswered
        return await real(group)

    c.client.get_volume = get_volume
    await c.async_turn_on(0, ceiling_db=0)
    amp.settle()

    assert amp.volume[0] == -70
    assert c.data.groups[0].volume_db == -70  # read after the power-up, not before
    assert not any(e.startswith("vol0:") for e in amp.log)


async def test_one_unanswered_read_before_the_zone_on_is_asked_again(
    hass: HomeAssistant,
) -> None:
    amp = FakeAmp()
    amp.turn_on_volume = -70
    amp.group_power[0] = False
    amp.volume[0] = -35
    c = make(hass, amp)
    amp.unanswered_volume = 1

    await c.async_turn_on(0, ceiling_db=0)
    amp.settle()

    assert amp.volume[0] == -35


async def test_a_turn_on_volume_landing_after_the_restore_is_overridden(
    hass: HomeAssistant,
) -> None:
    """A power-up slower than the wait: the restore is checked after a pause."""
    amp = FakeAmp()
    amp.turn_on_volume = -70
    amp.MUTE_CLEAR_LAG = 2  # lands between the restore's first read and its check
    amp.group_power[0] = False
    amp.volume[0] = -35
    c = make(hass, amp)

    async def slow_power_up() -> None:
        return None  # nothing has landed yet when the restore starts

    c._async_wait_for_power_up = slow_power_up
    await c.async_turn_on(0, ceiling_db=0)
    amp.settle()

    assert amp.volume[0] == -35
    assert amp.log.count("vol0:-35") >= 1


async def test_a_restore_that_cannot_be_sent_leaves_the_zone_on_and_silent(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Not a failed switch-on: the zone is on, at its silent power-up level."""
    amp = FakeAmp()
    amp.turn_on_volume = -70
    amp.group_power[0] = False
    amp.volume[0] = -35
    amp.fail_next("vol0:-35", times=5)
    c = make(hass, amp)

    await c.async_turn_on(0, ceiling_db=0)
    amp.settle()

    assert amp.group_power[0] is True
    assert amp.volume[0] == -70
    assert c.data.group_power[0] is True
    assert c.data.groups[0].volume_db == -70
    assert "Could not restore zone A's volume" in caplog.text


async def test_a_restore_that_never_holds_is_reported(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    amp = FakeAmp()
    amp.turn_on_volume = -70
    amp.group_power[0] = False
    amp.volume[0] = -35
    c = make(hass, amp)
    for _ in range(5):
        amp.lose_next("vol0:-35")

    await c.async_turn_on(0, ceiling_db=0)
    amp.settle()

    assert amp.volume[0] == -70
    assert c.data.groups[0].volume_db == -70
    assert any(
        r.levelname == "WARNING" and "did not hold" in r.getMessage()
        for r in caplog.records
    )


async def test_a_wake_also_restores_the_level(hass: HomeAssistant) -> None:
    amp = FakeAmp(master=False)
    amp.turn_on_volume = -70
    for g in amp.group_power:
        amp.group_power[g] = False
    amp.volume[0] = -35
    c = make(hass, amp)

    await c.async_turn_on(0, ceiling_db=0)
    amp.settle()

    assert amp.volume[0] == -35
    assert amp.mute[0] is False


async def test_a_wake_holds_a_muted_zones_level(hass: HomeAssistant) -> None:
    amp = FakeAmp(master=False)
    amp.turn_on_volume = -70
    for g in amp.group_power:
        amp.group_power[g] = False
    amp.volume[0] = -35
    amp.mute[0] = True
    c = make(hass, amp)

    await c.async_turn_on(0, ceiling_db=0)
    amp.settle()

    assert amp.mute[0] is True
    assert c.held_level(0) == -35


async def test_a_zone_already_on_is_not_restored(hass: HomeAssistant) -> None:
    amp = FakeAmp()
    amp.volume[0] = -35
    c = make(hass, amp)

    await c.async_turn_on(0, ceiling_db=-50)

    assert amp.volume[0] == -35
    assert not any(e.startswith("vol0:") for e in amp.log)



async def test_a_stale_cache_is_never_restored(hass: HomeAssistant) -> None:
    """If the amp will not say, the zone stays at its silent turn-on level."""
    amp = FakeAmp()
    amp.turn_on_volume = -70
    amp.group_power[0] = False
    amp.volume[0] = -50
    c = make(hass, amp)
    c.data.groups[0] = replace(c.data.groups[0], volume_db=-20)  # louder, stale
    amp.unanswered_volume = 2

    await c.async_turn_on(0, ceiling_db=0)
    amp.settle()

    assert amp.volume[0] == -70
    assert not any(e.startswith("vol0:") for e in amp.log)


async def test_the_level_is_read_before_the_wake(hass: HomeAssistant) -> None:
    """Whether a wake resets zone volumes is unmeasured; read it first."""
    amp = FakeAmp(master=False)
    amp.turn_on_volume = -70
    amp.wake_applies_turn_on_volume = True
    for g in amp.group_power:
        amp.group_power[g] = False
    amp.volume[0] = -35
    c = make(hass, amp)

    await c.async_turn_on(0, ceiling_db=0)
    amp.settle()

    assert amp.volume[0] == -35


async def test_the_zone_is_shown_on_before_the_restore_makes_it_audible(
    hass: HomeAssistant,
) -> None:
    amp = FakeAmp()
    amp.turn_on_volume = -70
    amp.group_power[0] = False
    amp.volume[0] = -35
    c = make(hass, amp)
    c.data.group_power[0] = False
    seen: list[bool | None] = []
    real = amp.set_volume

    async def set_volume(group: int, db: int) -> None:
        seen.append(c.data.group_power.get(group))
        await real(group, db)

    c.client.set_volume = set_volume
    await c.async_turn_on(0, ceiling_db=0)

    assert seen and all(seen)
    assert c.volume_verified(0)
    assert c.data.groups[0].volume_db == -35


async def test_the_power_up_wait_is_real(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The seam FakeAmp replaces still waits the configured time in production."""
    amp = FakeAmp()
    c = make(hass, amp)
    monkeypatch.setattr(coord_mod, "VOLUME_RESTORE_AFTER", 0.05)
    loop = asyncio.get_running_loop()

    start = loop.time()
    await SonanceCoordinator._async_wait_for_power_up(c)

    assert loop.time() - start >= 0.05



# ---------------------------------------------------------------------------
# A volume change un-mutes a zone (measured 2026-09-27), so a muted zone is
# never sent one: its level is held, shown, and applied when it is unmuted.
# ---------------------------------------------------------------------------


async def test_a_volume_set_on_a_muted_zone_is_held_not_sent(
    hass: HomeAssistant,
) -> None:
    amp = FakeAmp()
    amp.mute[0] = True
    c = make(hass, amp)
    z = zone(c, hass)

    await z.async_set_volume_level(0.5)  # -35 dB

    assert amp.mute[0] is True
    assert not any(e.startswith("vol0:") for e in amp.log)
    assert c.held_level(0) == -35
    assert c.data.groups[0].volume_db == -35
    assert c.data.groups[0].muted is True


async def test_a_volume_set_on_an_unmuted_zone_is_sent(hass: HomeAssistant) -> None:
    amp = FakeAmp()
    c = make(hass, amp)
    z = zone(c, hass)

    await z.async_set_volume_level(0.5)

    assert amp.volume[0] == -35
    assert c.held_level(0) is None


async def test_a_volume_set_whose_mute_cannot_be_read_is_refused(
    hass: HomeAssistant,
) -> None:
    """Not sent (it would un-mute), and not reported as done either."""
    amp = FakeAmp()
    c = make(hass, amp)
    amp.unanswered_mute = 2
    z = zone(c, hass)

    with pytest.raises(HomeAssistantError) as err:
        await z.async_set_volume_level(0.5)

    assert err.value.translation_key == "mute_unknown"
    assert not any(e.startswith("vol0:") for e in amp.log)
    assert c.held_level(0) is None


async def test_one_unanswered_mute_read_is_asked_again(hass: HomeAssistant) -> None:
    amp = FakeAmp()
    c = make(hass, amp)
    amp.unanswered_mute = 1
    z = zone(c, hass)

    await z.async_set_volume_level(0.5)

    assert amp.volume[0] == -35


async def test_a_hold_records_the_mute_it_read(hass: HomeAssistant) -> None:
    """Muted at a keypad since the last poll: HA must show it muted."""
    amp = FakeAmp()
    amp.mute[0] = True
    c = make(hass, amp)
    c.data.groups[0] = replace(c.data.groups[0], muted=False)  # stale
    z = zone(c, hass)

    await z.async_set_volume_level(0.5)

    assert c.data.groups[0].muted is True
    assert c.held_level(0) == -35


@pytest.mark.parametrize("delta", [+1, -1])
async def test_a_volume_step_on_a_muted_zone_moves_the_held_level(
    hass: HomeAssistant, delta: int
) -> None:
    amp = FakeAmp()
    amp.mute[0] = True
    c = make(hass, amp)
    c._held_level[0] = -35
    z = zone(c, hass)

    await (z.async_volume_up() if delta > 0 else z.async_volume_down())

    assert amp.mute[0] is True
    assert not any(e.startswith("vol0:") for e in amp.log)
    assert c.held_level(0) == -35 + delta


async def test_a_volume_step_on_a_muted_zone_without_a_hold_starts_from_the_amp(
    hass: HomeAssistant,
) -> None:
    amp = FakeAmp()
    amp.mute[0] = True
    amp.volume[0] = -40
    c = make(hass, amp)
    z = zone(c, hass)

    await z.async_volume_up()

    assert amp.mute[0] is True
    assert c.held_level(0) == -39


async def test_unmuting_applies_the_held_level(hass: HomeAssistant) -> None:
    amp = FakeAmp()
    amp.turn_on_volume = -70
    amp.group_power[0] = False
    amp.volume[0] = -35
    amp.mute[0] = True
    c = make(hass, amp)
    await c.async_turn_on(0, ceiling_db=0)
    amp.settle()
    z = zone(c, hass)

    await z.async_mute_volume(False)

    assert amp.mute[0] is False
    assert amp.volume[0] == -35
    assert amp.log.index("vol0:-35") < amp.log.index("mute0:off")
    assert c.held_level(0) is None
    assert c.data.groups[0].volume_db == -35


async def test_unmuting_without_a_hold_only_unmutes(hass: HomeAssistant) -> None:
    amp = FakeAmp()
    amp.mute[0] = True
    c = make(hass, amp)
    z = zone(c, hass)

    await z.async_mute_volume(False)

    assert amp.mute[0] is False
    assert not any(e.startswith("vol0:") for e in amp.log)


async def test_muting_keeps_a_held_level(hass: HomeAssistant) -> None:
    amp = FakeAmp()
    amp.mute[0] = True
    c = make(hass, amp)
    c._held_level[0] = -35
    z = zone(c, hass)

    await z.async_mute_volume(True)

    assert c.held_level(0) == -35


async def test_a_poll_shows_the_held_level_while_muted(hass: HomeAssistant) -> None:
    amp = FakeAmp()
    amp.mute[0] = True
    amp.volume[0] = -70
    c = make(hass, amp)
    c._held_level[0] = -35

    c.data = await c._async_update_data()

    assert c.data.groups[0].volume_db == -35
    assert c.held_level(0) == -35


async def test_a_zone_unmuted_outside_ha_drops_its_hold(hass: HomeAssistant) -> None:
    """The amp's level is the truth once it is playing."""
    amp = FakeAmp()
    amp.volume[0] = -70
    c = make(hass, amp)
    c._held_level[0] = -35

    c.data = await c._async_update_data()

    assert c.held_level(0) is None
    assert c.data.groups[0].volume_db == -70


# ---------------------------------------------------------------------------
# Holds across switch-ons, failures and polls (review of 0.3.3)
# ---------------------------------------------------------------------------


async def test_a_hold_survives_an_off_on_cycle(hass: HomeAssistant) -> None:
    amp = FakeAmp()
    amp.turn_on_volume = -70
    amp.group_power[0] = False
    amp.volume[0] = -35
    amp.mute[0] = True
    c = make(hass, amp)
    await c.async_turn_on(0, ceiling_db=0)
    amp.settle()
    await c.async_turn_off(0)

    await c.async_turn_on(0, ceiling_db=0)
    amp.settle()

    assert c.held_level(0) == -35
    await zone(c, hass).async_mute_volume(False)
    assert amp.volume[0] == -35
    assert amp.mute[0] is False


async def test_a_level_held_while_off_is_used_at_switch_on(hass: HomeAssistant) -> None:
    amp = FakeAmp()
    amp.group_power[0] = False
    amp.volume[0] = -20
    amp.mute[0] = True
    c = make(hass, amp)
    await zone(c, hass).async_set_volume_level(0.5)  # -35, held: muted
    assert c.held_level(0) == -35

    await c.async_turn_on(0, ceiling_db=0)
    amp.settle()

    assert c.held_level(0) == -35


async def test_an_unmuted_switch_on_drops_a_stale_hold(hass: HomeAssistant) -> None:
    amp = FakeAmp()
    amp.group_power[0] = False
    amp.volume[0] = -20
    c = make(hass, amp)
    c._held_level[0] = -35

    await c.async_turn_on(0, ceiling_db=0)
    amp.settle()

    assert c.held_level(0) is None
    assert amp.volume[0] == -20


@pytest.mark.parametrize(
    ("fault", "shown_muted"),
    [
        ("volume_echo_lost", None),
        ("unmute_not_sent", None),
        ("volume_not_sent", True),
    ],
)
async def test_a_failed_unmute_is_honest(
    hass: HomeAssistant, fault: str, shown_muted: bool | None
) -> None:
    """Never "muted" while it may be playing; the hold stays until a poll says."""
    amp = FakeAmp()
    amp.mute[0] = True
    c = make(hass, amp)
    c._held_level[0] = -35
    c.data.groups[0] = replace(c.data.groups[0], muted=True)
    if fault == "volume_echo_lost":
        amp.lose_echo_next("vol0:-35")
    elif fault == "unmute_not_sent":
        amp.not_sent_next("mute0:off")
    else:
        amp.not_sent_next("vol0:-35")

    with pytest.raises(HomeAssistantError):
        await zone(c, hass).async_mute_volume(False)

    assert c.data.groups[0].muted is shown_muted
    assert c.held_level(0) == -35


async def test_the_restore_holds_if_the_zone_was_muted_meanwhile(
    hass: HomeAssistant,
) -> None:
    """A keypad mute after the power-up window: the restore must not undo it."""
    amp = FakeAmp()
    amp.turn_on_volume = -70
    amp.group_power[0] = False
    amp.volume[0] = -35
    c = make(hass, amp)

    async def power_up_then_keypad_mute() -> None:
        amp.settle()
        amp.mute[0] = True

    c._async_wait_for_power_up = power_up_then_keypad_mute
    await c.async_turn_on(0, ceiling_db=0)

    assert amp.mute[0] is True
    assert not any(e.startswith("vol0:") for e in amp.log)
    assert c.held_level(0) == -35
    assert c.data.groups[0].muted is True


async def test_the_fail_safe_holds_the_level(hass: HomeAssistant) -> None:
    amp = FakeAmp()
    amp.turn_on_volume = -70
    amp.group_power[0] = False
    amp.volume[0] = -35
    amp.mute[0] = True
    amp.lose_echo_next("group0:on")
    c = make(hass, amp)

    with pytest.raises(SonanceConnectionError):
        await c.async_turn_on(0, ceiling_db=0)
    amp.settle()

    assert amp.mute[0] is True
    assert c.held_level(0) == -35
    assert c.data.groups[0].volume_db == -35


async def test_the_already_on_path_shows_the_hold(hass: HomeAssistant) -> None:
    amp = FakeAmp()
    amp.mute[0] = True
    amp.volume[0] = -70
    c = make(hass, amp)
    c._held_level[0] = -35

    await c.async_turn_on(0, ceiling_db=0)

    assert c.data.groups[0].volume_db == -35


async def test_a_poll_with_the_mute_unknown_shows_the_amps_level(
    hass: HomeAssistant,
) -> None:
    amp = FakeAmp()
    amp.volume[0] = -20
    c = make(hass, amp)
    c._held_level[0] = -35
    real = amp.read_group

    async def read_group(group: int) -> GroupState:
        state = await real(group)
        return replace(state, muted=None) if group == 0 else state

    c.client.read_group = read_group
    c.data = await c._async_update_data()

    assert c.data.groups[0].volume_db == -20
    assert c.held_level(0) == -35


async def test_a_poll_does_not_make_an_unanswered_group_look_answered(
    hass: HomeAssistant,
) -> None:
    amp = FakeAmp()
    amp.mute[0] = True
    c = make(hass, amp)
    c._held_level[0] = -35
    real = amp.read_group

    async def read_group(group: int) -> GroupState:
        state = await real(group)
        return replace(state, volume_db=None) if group == 0 else state

    c.client.read_group = read_group
    c.data = await c._async_update_data()

    assert c.data.groups[0].volume_db is None


async def test_a_dropped_hold_stays_dropped_after_a_restart(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    amp = FakeAmp()
    c = make(hass, amp)
    c._set_hold(0, -35)
    await _flush_store(hass)
    c._drop_hold(0)
    await _flush_store(hass)

    again = make(hass, amp, entry=c.config_entry)
    await again.async_load_holds()

    assert again.held_level(0) is None


async def test_a_settled_mute_stays_settled_after_a_restart(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    amp = FakeAmp()
    c = make(hass, amp)
    c._owe_mute(1)
    await _flush_store(hass)
    c._settle_mute(1)
    await _flush_store(hass)

    again = make(hass, amp, entry=c.config_entry)
    await again.async_load_holds()

    assert again._mute_owed == set()

"""Data update coordinator for the Sonance DSP integration."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field, replace
from datetime import timedelta
from functools import partial
from typing import NoReturn

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import (
    AMP_POWER_MISSES_BRIDGED,
    AMP_POWER_READ_ATTEMPTS,
    AMP_POWER_READ_INTERVAL,
    CLOSE_WAIT,
    CONF_SCAN_INTERVAL,
    DEFAULT_HTTP_PORT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    GROUP_LETTERS,
    GROUP_POWER_STALE_AFTER,
    MAX_GROUPS,
    MIN_VOLUME_DB,
    MUTE_VERIFY_DELAYS,
    MUTE_VERIFY_DELAYS_AFTER_WAKE,
    POLL_RETRY_AFTER,
    POWER_CONFIRM_INTERVAL,
    POWER_CONFIRM_TIMEOUT,
    VOLUME_CONFIRM_DELAYS,
    VOLUME_RESTORE_AFTER,
    WAKE_POLL_INTERVAL,
    WAKE_RETRY_HOLDOFF,
    WAKE_TIMEOUT,
)
from .http_api import AmplifierIdentity, SonanceHttpApi, SonanceHttpError, Topology
from .protocol import (
    GroupState,
    SonanceConnectionError,
    SonanceError,
    SonanceNotSentError,
    SonanceProtocol,
)

_LOGGER = logging.getLogger(__name__)

type SonanceConfigEntry = ConfigEntry[SonanceCoordinator]


@dataclass
class SonanceData:
    """One poll's worth of amplifier state."""

    identity: AmplifierIdentity
    topology: Topology | None
    groups: dict[int, GroupState] = field(default_factory=dict)
    group_power: dict[int, bool] = field(default_factory=dict)
    amp_power: bool | None = None


# Marks "leave this field as it is" where None itself means "unknown".
_UNCHANGED = object()


class _PowerOp:
    """One power change in progress."""

    def __init__(self) -> None:
        # Set when the caller is cancelled. Honoured only up to the first
        # zone-on: before that nothing audible has happened and the request can
        # simply be dropped; after it the sequence has to finish, because
        # stopping between a zone-on and its mute restore leaves a zone
        # playing unmuted.
        self.abandoned = False
        # Set by the first status-page read that fails. The rest of the
        # operation then does without the page instead of waiting out a hung
        # one again at every read.
        self.http_failed = False


class SonanceCoordinator(DataUpdateCoordinator[SonanceData]):
    """Polls the amplifier over TCP, with HTTP filling what TCP cannot answer.

    Polling rather than push, and that was tested rather than assumed: a socket
    held idle across an out-of-band volume change received nothing, while a
    control query on the same socket answered in 10 ms. See docs/protocol.md.

    The one untested vector is audio sense, which is what the sibling Triad
    device pushes. It would not change this: an audio-sense event says a source
    woke up, not what the volume is.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: SonanceConfigEntry,
        client: SonanceProtocol,
        identity: AmplifierIdentity,
        host: str,
    ) -> None:
        interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=identity.name,
            update_interval=timedelta(seconds=interval),
        )
        self.client = client
        self.identity = identity
        self.http = SonanceHttpApi(
            async_get_clientsession(hass), host, DEFAULT_HTTP_PORT
        )
        self.groups: list[int] = []
        self._topology: Topology | None = None
        self._http_available = True
        # Held for the whole of every write, and for the whole of a power
        # change -- which can include a ten-second wake. Two things need it.
        # A scene switching several zones on must share one wake rather than
        # each sending its own power-on mid-boot. And Assist's relative-volume
        # intent calls entity methods directly, bypassing PARALLEL_UPDATES, so
        # without a lock of the coordinator's own a volume step sent during a
        # wake is dropped by the booting amplifier while echoing success.
        self.command_lock = asyncio.Lock()
        # Bumped by every locally applied change. A poll that was already
        # reading when one landed returns what it started with instead of its
        # own results, which predate the change: without this a poll in flight
        # overwrites a turn-on's read-back with the pre-turn-on volume, and the
        # next volume_up steps from the wrong baseline.
        self._generation = 0
        self._group_power_at: float | None = None
        self._amp_power_misses = 0
        self._wake_failed_at: float | None = None
        self._closing = False
        # Zones whose mute a failed switch-on may have cleared on the amp, and
        # which must come back muted however the amp reports them.
        self._mute_owed: set[int] = set()
        # Zones whose cached volume may predate their last switch-on.
        self._volume_unverified: set[int] = set()

    async def async_discover(self) -> None:
        """Enumerate populated groups and read the channel layout.

        Runs once at setup, after the connection has settled: connection churn
        produces the same empty replies as an absent group, so discovering on a
        fresh socket can miss zones that exist.

        The TCP enumeration is cross-checked against the HTTP channel-to-group
        map, which is authoritative. They can only disagree if a reply arrived
        late and shifted the stream -- the protocol layer's group-letter check
        catches most of that, but silence from a slow group is invisible to it.
        On disagreement the HTTP answer wins, because it is a direct statement
        of the topology rather than an inference from who answered.
        """
        try:
            self._topology = await self.http.topology()
        except SonanceHttpError as err:
            # Not fatal. Without it we lose device-supplied zone names and the
            # cross-check, but the TCP enumeration still finds the zones.
            _LOGGER.warning(
                "Could not read channel layout over HTTP (%s); falling back to "
                "generic zone names and unverified enumeration",
                err,
            )
            self._http_available = False

        tcp_groups = await self.client.discover_groups()

        if self._topology is not None and self._topology.output_groups:
            http_groups = [
                g for g in range(MAX_GROUPS) if self._topology.group_members(g)
            ]
            if set(http_groups) != set(tcp_groups):
                _LOGGER.warning(
                    "Group enumeration disagrees: the amplifier answered on %s "
                    "but its channel map says %s. Using the channel map",
                    [GROUP_LETTERS[g] for g in tcp_groups],
                    [GROUP_LETTERS[g] for g in http_groups],
                )
            self.groups = http_groups
        else:
            self.groups = tcp_groups

        _LOGGER.debug(
            "Discovered groups: %s", [GROUP_LETTERS[g] for g in self.groups]
        )

    def apply_optimistic(self, group: int, **fields: object) -> None:
        """Update one group's cached state and push it to entities now.

        Without this the UI waits a whole poll interval after a write, and the
        coordinator's refresh debounce makes a dragged slider visibly snap back
        to its old value before settling on the new one.
        """
        if self.data is None:
            return
        current = self.data.groups.get(group)
        if current is None:
            return
        self.data.groups[group] = replace(current, **fields)
        if fields.get("volume_db") is not None:
            self._volume_unverified.discard(group)
        self._generation += 1
        self.async_set_updated_data(self.data)

    def group_name(self, group: int) -> str | None:
        return self._topology.group_name(group) if self._topology else None

    def input_names(self) -> list[str]:
        return self._topology.input_names if self._topology else []

    def source_number_for_input_name(self, name: str) -> int | None:
        """Resolve a source name to its 1-based number.

        Goes through the topology rather than the TCP reply because the reply's
        ``Src1=`` digit is a fixed label -- it stays 1 whatever is selected.
        Returns None when the channel layout could not be read at all, which is
        the one case where source selection has to be unavailable rather than
        wrong.
        """
        if self._topology is None:
            return None
        return self._topology.source_number_for_input_name(name)

    def maximum_db(self, group: int) -> int | None:
        """The amplifier's own ceiling for a group, if it reported one."""
        return self._topology.maximum_db(group) if self._topology else None

    def gain_offset(self, group: int) -> int | None:
        """Installer gain trim for a group, in dB, if the members agree."""
        if self._topology is None:
            return None
        values = []
        for i in self._topology.group_members(group):
            if i < len(self._topology.gain_offset):
                try:
                    values.append(int(self._topology.gain_offset[i]))
                except ValueError:
                    return None
        if not values or len(set(values)) != 1:
            # Left and right trimmed differently is a real possibility and not
            # something a single zone-level number can represent honestly.
            return None
        return values[0]

    async def _async_update_data(self) -> SonanceData:
        if self.command_lock.locked() and self.data is not None:
            # A command is mid-flight, possibly a ten-second wake, and its own
            # read-back will be fresher than anything read now. This check only
            # stops a poll STARTING during a command; one already running is
            # handled by the generation check below, and the one write pair
            # that must not be split -- a zone-on and its mute restore -- is
            # sent under a single hold of the protocol's own lock.
            if not self.last_update_success:
                # Returning cached data would count as a successful poll and
                # mark the zones available again without reading anything.
                raise UpdateFailed("A command is in progress")
            return self.data
        generation = self._generation

        try:
            groups, amp_power = await self._async_read_tcp()
        except SonanceConnectionError:
            # One late reply tears the socket down, and Home Assistant silently
            # skips an unavailable entity in a service call -- so one blip
            # would turn a turn_off into a no-op for a whole interval. Ask
            # again on a fresh connection before calling the amp unreachable.
            try:
                groups, amp_power = await self._async_read_tcp()
            except SonanceConnectionError as err:
                raise UpdateFailed(
                    f"Amplifier unreachable: {err}", retry_after=POLL_RETRY_AFTER
                ) from err

        group_power: dict[int, bool] = {}
        try:
            group_power = await self.http.group_power()
            self._group_power_at = self._now()
            if not self._http_available:
                _LOGGER.info("HTTP status page is reachable again")
                self._http_available = True
        except SonanceHttpError as err:
            # Deliberately not an UpdateFailed. Losing the HTTP page costs
            # group power, not volume control, and taking every zone entity
            # unavailable over it would be a worse outcome than a missing
            # attribute.
            if self._http_available:
                _LOGGER.info("HTTP status page unavailable (%s)", err)
                self._http_available = False
            if (
                self.data is not None
                and self._group_power_at is not None
                and self._now() - self._group_power_at < self._group_power_stale_after
            ):
                group_power = dict(self.data.group_power)

        if generation != self._generation and self.data is not None:
            _LOGGER.debug("A change landed during the poll; keeping it")
            return self.data

        self._volume_unverified -= {
            g for g, state in groups.items() if state.volume_db is not None
        }
        return SonanceData(
            identity=self.identity,
            topology=self._topology,
            groups=groups,
            group_power=group_power,
            amp_power=amp_power,
        )

    async def _async_read_tcp(self) -> tuple[dict[int, GroupState], bool | None]:
        groups = {g: await self.client.read_group(g) for g in self.groups}
        amp_power = self._bridge_amp_power(await self._async_read_amp_power())
        return groups, amp_power

    @property
    def _group_power_stale_after(self) -> float:
        """Never less than three polls, so the longest interval still bridges one."""
        interval = self.update_interval.total_seconds() if self.update_interval else 0
        return max(GROUP_POWER_STALE_AFTER, 3 * interval)

    def _bridge_amp_power(self, amp_power: bool | None) -> bool | None:
        """Carry an "on" answer across a missed reply or two. Never "standby"."""
        if amp_power is not None:
            return amp_power
        previous = self.data.amp_power if self.data is not None else None
        if previous is not True or self._amp_power_misses >= AMP_POWER_MISSES_BRIDGED:
            return None
        self._amp_power_misses += 1
        return previous

    @staticmethod
    def _now() -> float:
        return asyncio.get_running_loop().time()

    # --- power ---------------------------------------------------------------
    #
    # Measured on this amplifier in Power Button mode -- see docs/protocol.md,
    # "Power":
    #
    #   * Switching a zone ON clears its mute. The clear was visible at the
    #     first sample, about half a second after the zone-on.
    #   * Standby and wake do NOT clear mute, and a switched-off zone still
    #     reports its mute -- so the amp itself is where mute is remembered.
    #   * A zone's on/off flag survives standby. The status page shows a zone
    #     "on" while the whole amp is in standby, so a zone is only really on
    #     when the amp is on AND the zone is on.
    #   * Waking from standby takes about 10 s. Mute writes sent before it
    #     finished were lost although they echoed success.
    #   * Switching every zone off does NOT put the amp in standby by itself.
    #
    # NOT measured, and written to be right either way rather than to depend
    # on:
    #
    #   * whether the amp always re-applies a mute sent with the zone-on at the
    #     end of the power-up window (seen every time, ~1.05 s) -- so the mute is
    #     read back across the window and re-sent;
    #   * whether a zone-on sent to a zone that is ALREADY on re-applies its
    #     turn-on volume and clears its mute -- so it is never sent to one;
    #   * whether standby accepts a zone-off -- so zones are switched off both
    #     before a wake and again after it;
    #   * what power-on does to an amplifier that is already on -- so it is
    #     only ever sent to one that has just reported standby.

    async def async_turn_on(self, group: int, *, ceiling_db: int = 0) -> None:
        """Switch a zone on, restoring its mute and then its volume.

        ``ceiling_db`` caps the restored volume: the zone's configured ceiling.

        The work runs in its own task. A caller cancelled before anything was
        switched cancels the request; one cancelled after the zone-on does not
        stop it, because switching a zone on clears its mute and stopping before
        the restore would leave the zone playing unmuted.
        """
        op = _PowerOp()
        task = asyncio.ensure_future(self._async_turn_on(group, op, ceiling_db))
        task.add_done_callback(partial(self._log_if_abandoned, group, op))
        try:
            # wait() rather than shield(): the task is not cancelled with the
            # caller either way, and its outcome is reported below rather than
            # as an anonymous "exception in shielded future".
            await asyncio.wait((task,))
        except asyncio.CancelledError:
            op.abandoned = True
            raise
        task.result()

    @staticmethod
    def _log_if_abandoned(group: int, op: _PowerOp, task: asyncio.Task[None]) -> None:
        if task.cancelled() or not op.abandoned:
            return
        if (err := task.exception()) is not None:
            _LOGGER.warning(
                "Switching zone %s on failed after the request was cancelled: %s",
                GROUP_LETTERS[group],
                err,
            )

    async def _async_turn_on(
        self, group: int, op: _PowerOp, ceiling_db: int
    ) -> None:
        async with self.command_lock:
            self._check_open()
            if op.abandoned:
                return
            amp_on = await self._async_read_amp_power_patiently()
            if amp_on is None:
                # Guessing is how zones nobody asked for start playing: a
                # power-on to an amp that is already on has never been
                # measured, and a zone-on to a zone already playing may reset
                # its volume. Status queries are the only thing sent so far.
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="amp_power_unknown",
                )
            if op.abandoned:
                return
            fresh = await self._async_read_group_power(op)
            flags = fresh
            if flags is None and self.data is not None:
                flags = self.data.group_power

            if amp_on:
                zone = flags.get(group) if flags else None
                if zone:
                    # Already on. Scenes and homeassistant.turn_on call this
                    # without checking, and a zone-on to a playing zone may
                    # reset it to its turn-on volume. Refresh and leave it.
                    self._apply_power(
                        group,
                        zone_on=True,
                        amp_on=True,
                        power=flags,
                        volume_db=await self._async_read_volume(group),
                        muted=await self.client.get_mute(group),
                    )
                    return
                if zone is None:
                    # The amp is on and nothing says whether this zone is: the
                    # status page is down and the last reading has expired. It
                    # may be playing, and a zone-on may reset it.
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        translation_key="zone_power_unknown",
                        translation_placeholders={"zone": GROUP_LETTERS[group]},
                    )

            known_off: set[int] = set()
            if not amp_on:
                self._check_wake_holdoff()
                woke = await self._async_wake_only(group, fresh, op)
                if woke is None:
                    return  # abandoned before anything but zone-offs was sent
                known_off = woke
                if op.abandoned:
                    # Awake, and the zones the wake revived are off again, but
                    # nobody wants this zone on any more.
                    self._publish_power(
                        known_off,
                        amp_on=True,
                        group_power=await self._async_read_group_power(op),
                    )
                    return

            restore = self._mute_to_restore(group, await self.client.get_mute(group))
            level = await self._async_level_to_restore(group, ceiling_db)
            if op.abandoned:
                return
            await self._async_switch_on(
                group, restore, op, known_off, after_wake=not amp_on, level=level
            )

    async def _async_switch_on(
        self,
        group: int,
        restore: bool,
        op: _PowerOp,
        known_off: set[int],
        *,
        after_wake: bool,
        level: int | None,
    ) -> None:
        """Send the zone-on, restore the mute and then the volume, and publish.

        From the first zone-on onwards nothing is abandoned, and any failure
        leaves the zone silent rather than playing: if the sequence breaks with
        the mute not yet confirmed, the zone is muted again and that checked,
        and switched off if even that cannot be done.
        """
        power: dict[int, bool] | None = None
        sent = False
        try:
            for attempt in (1, 2):
                try:
                    if restore:
                        # Zone-on and mute together, every time -- including a
                        # retry's, since every zone-on clears the mute.
                        await self.client.set_group_power_muted(group)
                    else:
                        await self.client.set_group_power(group, True)
                except SonanceNotSentError:
                    raise
                except SonanceError:
                    sent = True
                    raise
                sent = True
                power, confirmed = await self._async_await_group_power(
                    group, on=True, op=op
                )
                if confirmed or power is None:
                    break
                if attempt == 1:
                    self._log_retry(group, on=True)
            else:
                if restore:
                    await self._async_verify_mute(group, after_wake=after_wake)
                self._raise_not_confirmed(group, on=True)
            if restore:
                await self._async_verify_mute(group, after_wake=after_wake)
        except SonanceNotSentError:
            if sent:
                await self._async_fail_safe(group, restore, op, known_off)
            else:
                # Nothing reached the amplifier: the zone is as it was.
                self._publish_power(known_off, amp_on=True)
            raise
        except SonanceError:
            await self._async_fail_safe(group, restore, op, known_off)
            raise

        if power is None:
            # Status page down: the echoes are all there is. The zones the wake
            # switched off were each acknowledged, so show them off.
            power = dict(self.data.group_power) if self.data is not None else {}
            for other in known_off:
                power[other] = False
        if restore:
            self._mute_owed.discard(group)
        # The mute is what was sent and then verified; the volume is read back.
        self._apply_power(
            group,
            zone_on=True,
            amp_on=True,
            power=power,
            volume_db=await self._async_restore_level(group, level, waited=restore),
            muted=restore,
        )

    async def _async_wait_for_power_up(self) -> None:
        """Wait out a zone's power-up: turn-on volume at ~0.2 s, mute at ~1.05 s."""
        await asyncio.sleep(VOLUME_RESTORE_AFTER)

    async def _async_level_to_restore(self, group: int, ceiling_db: int) -> int | None:
        """The volume to put back after a zone-on: the zone's level while off.

        A switched-off zone still reports its volume, so this is read from the
        amplifier like the mute, and survives a restart; the last polled value
        is the fallback. Never above the ceiling. None when neither is known --
        the zone then stays at its turn-on level.
        """
        try:
            volume = await self.client.get_volume(group)
        except SonanceError:
            volume = None
        if volume is None and self.data is not None:
            state = self.data.groups.get(group)
            volume = state.volume_db if state is not None else None
        if volume is None:
            return None
        return max(MIN_VOLUME_DB, min(volume, ceiling_db))

    async def _async_restore_level(
        self, group: int, level: int | None, *, waited: bool
    ) -> int | None:
        """Put a switched-on zone back at ``level`` once its power-up has passed.

        The amplifier applies the zone's turn-on volume about 0.2 s after the
        zone-on, over anything sent before, so this waits out the power-up --
        the mute check already has, when there was one -- and then sets the
        level and reads it back. A restore that fails is logged, not raised: the
        zone is on, and at its turn-on level, which is the silent one when the
        amplifier is set up as recommended.
        """
        if level is None:
            return await self._async_read_volume(group)
        if not waited:
            await self._async_wait_for_power_up()
        letter = GROUP_LETTERS[group]
        current: int | None = None
        try:
            # Done only when the level still reads back after a pause, so a
            # power-up slower than expected cannot land its turn-on volume
            # after a check that had already passed.
            for delay in VOLUME_CONFIRM_DELAYS:
                if await self.client.get_volume(group) != level:
                    await self.client.set_volume(group, level)
                await asyncio.sleep(delay)
                current = await self.client.get_volume(group)
                if current == level:
                    self._volume_unverified.discard(group)
                    return level
        except SonanceError as err:
            _LOGGER.warning(
                "Could not restore zone %s's volume to %s dB (%s)", letter, level, err
            )
            return await self._async_read_volume(group)
        _LOGGER.warning(
            "Zone %s's volume did not hold at %s dB after switching on; it reads %s",
            letter,
            level,
            current,
        )
        return await self._async_read_volume(group)

    async def _async_fail_safe(
        self, group: int, restore: bool, op: _PowerOp, known_off: set[int]
    ) -> None:
        """After a switch-on broke part-way: leave the zone silent, say so honestly."""
        zone_on: bool | None = None
        muted: bool | object | None = _UNCHANGED
        if restore:
            zone_on, muted = await self._async_force_silent(group)
        power = await self._async_read_group_power(op)
        if power is None:
            power = dict(self.data.group_power) if self.data is not None else {}
            for other in known_off:
                power[other] = False
            power.pop(group, None)
            if zone_on is not None:
                power[group] = zone_on
        self._publish_power(
            set(), amp_on=True, group_power=power, group=group, muted=muted
        )

    async def _async_verify_mute(self, group: int, *, after_wake: bool) -> None:
        """Check a restored mute across the clear window, re-sending it if lost.

        The amplifier clears a zone's mute some time after switching it on,
        and whether a mute sent straight after the zone-on lands before or
        after that clear was never measured. So it is read back at intervals
        covering the window -- a longer one straight after a wake, when the
        zone may power up late -- and anything but a confirmed "muted" is
        answered with another mute. It must end on a confirmed read, or this
        raises and the caller makes the zone silent another way. The first time
        a mute is found lost, the log records when: the missing measurement.
        """
        delays = MUTE_VERIFY_DELAYS_AFTER_WAKE if after_wake else MUTE_VERIFY_DELAYS
        # Up to two extra reads if the window ends without a confirmation.
        schedule = (*delays, delays[-1], delays[-1])
        letter = GROUP_LETTERS[group]
        elapsed = 0.0
        confirmed = False
        for i, delay in enumerate(schedule):
            if i >= len(delays) and confirmed:
                return
            await asyncio.sleep(delay)
            elapsed += delay
            state = await self.client.get_mute(group)
            if state is True:
                confirmed = True
                continue
            if state is False:
                _LOGGER.info(
                    "Zone %s lost its mute %.1f s after switching on; muting it again",
                    letter,
                    elapsed,
                )
            await self.client.set_mute(group, True)
            confirmed = False
        if not confirmed:
            raise SonanceConnectionError(
                f"Zone {letter}'s mute could not be confirmed after switching on"
            )

    async def _async_force_silent(self, group: int) -> tuple[bool | None, bool | None]:
        """Best effort to leave a zone silent after a failed switch-on.

        Returns what is then known of the zone's power and mute. First a mute,
        checked across the clear window like any restore. If that cannot be
        confirmed, the zone is switched off -- and muted once more while off,
        where no pending clear can undo it, so the next switch-on restores it.
        Either way the mute is recorded as owed until it is known to be back.
        """
        letter = GROUP_LETTERS[group]
        self._mute_owed.add(group)
        try:
            await self.client.set_mute(group, True)
            await self._async_verify_mute(group, after_wake=False)
        except SonanceError:
            pass
        else:
            _LOGGER.warning(
                "Switching zone %s on failed part-way; it has been muted", letter
            )
            self._mute_owed.discard(group)
            return None, True
        try:
            await self.client.set_group_power(group, False)
        except SonanceError:
            _LOGGER.error(
                "Switching zone %s on failed part-way, and it could be neither "
                "muted nor switched off; it may be playing unmuted",
                letter,
            )
            return None, None
        with contextlib.suppress(SonanceError):
            await self.client.set_mute(group, True)
        _LOGGER.warning(
            "Switching zone %s on failed part-way and it could not be muted; it "
            "has been switched off",
            letter,
        )
        return False, None

    async def _async_read_volume(self, group: int) -> int | None:
        """A zone's volume, asking twice: one late reply is routine.

        Best effort: a switch-on that worked must still be published even if
        its volume cannot be read. An unread volume is marked unverified rather
        than left looking like the pre-switch-on level, which Assist would
        otherwise step from.
        """
        volume: int | None = None
        for _ in (1, 2):
            try:
                volume = await self.client.get_volume(group)
            except SonanceError:
                volume = None
            if volume is not None:
                self._volume_unverified.discard(group)
                return volume
        self._volume_unverified.add(group)
        return None

    def volume_verified(self, group: int) -> bool:
        """False while the cached volume may predate the last switch-on."""
        return group not in self._volume_unverified

    def note_user_mute(self, group: int) -> None:
        """The user set the mute themselves, so none is owed any more."""
        self._mute_owed.discard(group)

    def _mute_to_restore(self, group: int, was_muted: bool | None) -> bool:
        """Whether to mute a zone after switching it on.

        Read from the amplifier while the zone is still off -- it keeps the
        mute -- so it survives a Home Assistant restart. A mute owed from a
        switch-on that failed part-way wins over the read, because that
        failure may have cleared it on the amplifier. When the read fails, the
        last polled value; when there is none, muted. A zone that comes up
        silent is one tap to fix. One that comes up playing when it should not
        is the failure Home Assistant owning power exists to prevent.
        """
        letter = GROUP_LETTERS[group]
        if group in self._mute_owed:
            _LOGGER.info("Restoring zone %s's mute from a failed switch-on", letter)
            return True
        if was_muted is not None:
            return was_muted
        state = self.data.groups.get(group) if self.data is not None else None
        if state is not None and state.muted is not None:
            _LOGGER.warning(
                "Could not read zone %s's mute before switching it on; "
                "restoring the last known value (%s)",
                letter,
                "muted" if state.muted else "unmuted",
            )
            return state.muted
        _LOGGER.warning(
            "Could not read zone %s's mute before switching it on, and no "
            "earlier value is known; switching it on muted",
            letter,
        )
        return True

    async def async_turn_off(self, group: int) -> None:
        """Switch a zone off, and the amplifier too if it was the last one on."""
        async with self.command_lock:
            self._check_open()
            op = _PowerOp()
            amp_on = await self._async_read_amp_power_patiently()
            if amp_on is False:
                # Already silent. Clear the flag anyway, so a later wake does
                # not bring the zone back -- but best effort, since whether
                # standby accepts it is unmeasured, and failing a request for
                # silence on a silent zone would help nobody. What is shown is
                # what the status page says: without it the flag is unknown,
                # since an echo in standby is not proof it cleared.
                await self._async_switch_off_in_standby(group)
                power = await self._async_read_group_power(op)
                if power is None:
                    unknown = dict(self.data.group_power) if self.data else {}
                    unknown.pop(group, None)
                    self._publish_power(set(), amp_on=False, group_power=unknown)
                    return
                if power.get(group):
                    _LOGGER.info(
                        "Zone %s is still flagged on in standby; a wake from "
                        "outside Home Assistant would bring it back",
                        GROUP_LETTERS[group],
                    )
                self._apply_power(
                    group, zone_on=bool(power.get(group)), amp_on=False, power=power
                )
                return

            error: SonanceError | None = None
            for attempt in (1, 2):
                try:
                    await self.client.set_group_power(group, False)
                except SonanceError as err:
                    # Maybe applied with its echo lost: the page will say.
                    error = err
                else:
                    error = None
                power, confirmed = await self._async_await_group_power(
                    group, on=False, op=op
                )
                if confirmed:
                    break
                if power is None:
                    if error is not None:
                        raise error
                    break
                if attempt == 1:
                    self._log_retry(group, on=False)
            else:
                if error is not None:
                    raise error
                self._raise_not_confirmed(group, on=False)

            if power is None:
                # Without the status page there is no way to know whether
                # another zone is still on, and standby would silence it.
                _LOGGER.debug(
                    "Status page unavailable; not putting the amp in standby"
                )
            elif not any(power.get(g) for g in self.groups if g != group):
                # Sent whatever the cache says: a cached "standby" can be
                # stale. (A standby sent to a sleeping amp was not measured,
                # but by then nothing is playing for it to disturb.)
                await self.client.set_amp_power(False)
                amp_on = False
            self._apply_power(group, zone_on=False, amp_on=amp_on, power=power)

    async def _async_wake_only(
        self, group: int, flags: dict[int, bool] | None, op: _PowerOp
    ) -> set[int] | None:
        """Wake the amplifier so that only ``group`` comes back.

        Zone flags survive standby, and a wake brings back every zone whose
        flag is set -- which is every zone that was on when something other
        than Home Assistant put the amp to sleep. While in standby they are all
        shown off, so switching them off loses nothing, and turning on one zone
        must not bring back three. The requested zone is switched off too if
        flagged, so that its zone-on is the measured off-to-on case and applies
        its turn-on volume.

        They are switched off before the wake, so nothing plays at all if
        standby accepts it, and again after it whatever the standby page said,
        in case standby echoed a zone-off it did not keep. Without the status
        page every zone is switched off, since there is no telling which are
        flagged. If standby does not answer a zone-off at all, the rest wait
        for the wake: there is no sense waiting out a timeout for each.

        Returns the zones known to be off -- acknowledged after the wake -- or
        None if the caller went away before the power-on was sent. Once it has
        been sent this finishes regardless.
        """
        targets = [g for g in self.groups if flags is None or flags.get(g) is not False]
        for zone in targets:
            if not await self._async_switch_off_in_standby(zone):
                break
        after = await self._async_read_group_power(op)
        if after is not None and (still := [g for g in targets if after.get(g)]):
            _LOGGER.debug(
                "Zones %s still flagged on in standby; switching them off "
                "again once awake",
                [GROUP_LETTERS[g] for g in still],
            )
        if op.abandoned:
            return None
        await self._async_wake()

        switched_off: set[int] = set()
        pending = list(targets)
        error: SonanceError | None = None
        for _ in (1, 2):
            failed: list[int] = []
            for zone in pending:
                try:
                    await self.client.set_group_power(zone, False)
                except SonanceError as err:
                    failed.append(zone)
                    error = err
                else:
                    switched_off.add(zone)
            pending = failed
            if not pending:
                return switched_off

        # A zone the wake revived may be playing and could not be switched
        # off. Publish what is known before raising, so it does not keep
        # showing off: the status page if it answers, otherwise unknown.
        now = await self._async_read_group_power(op)
        if now is None:
            now = dict(self.data.group_power) if self.data is not None else {}
            for zone in pending:
                now.pop(zone, None)
            for zone in switched_off:
                now[zone] = False
        self._publish_power(set(), amp_on=True, group_power=now)
        assert error is not None
        raise error

    async def _async_switch_off_in_standby(self, group: int) -> bool:
        """Clear a zone's flag while the amp sleeps, if standby allows it.

        Whether standby echoes or even accepts a zone command is unmeasured,
        so a failure here is logged and passed over: what follows either does
        not need it or does it again once the amplifier is awake. Returns
        whether it was acknowledged.
        """
        try:
            await self.client.set_group_power(group, False)
        except SonanceError as err:
            _LOGGER.debug(
                "Zone %s off in standby was not acknowledged (%s)",
                GROUP_LETTERS[group],
                err,
            )
            return False
        return True

    def _check_wake_holdoff(self) -> None:
        """Refuse a wake straight after one that failed, before sending anything."""
        if (
            self._wake_failed_at is not None
            and self._now() - self._wake_failed_at < WAKE_RETRY_HOLDOFF
        ):
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="amp_wake_recently_failed",
                translation_placeholders={"seconds": str(int(WAKE_RETRY_HOLDOFF))},
            )

    async def _async_wake(self) -> None:
        """Wake the amplifier from standby and wait until it has finished.

        Only status queries are sent while it boots: mute writes sent in that
        window were lost although they echoed success.
        """
        _LOGGER.debug("Waking amplifier from standby")
        try:
            await self.client.set_amp_power(True)
        except SonanceError as err:
            # Maybe applied with its echo lost. Power-on is only ever sent
            # straight after a read of standby, so waiting to see is safe --
            # and giving up here would skip the zone-offs after the wake.
            _LOGGER.debug("Power-on not acknowledged (%s); waiting to see", err)
        deadline = self._now() + WAKE_TIMEOUT
        while self._now() < deadline:
            await asyncio.sleep(WAKE_POLL_INTERVAL)
            if await self._async_read_amp_power():
                return
        self._wake_failed_at = self._now()
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="amp_did_not_wake",
            translation_placeholders={"seconds": str(int(WAKE_TIMEOUT))},
        )

    async def _async_read_amp_power(self) -> bool | None:
        on = await self.client.get_amp_power()
        if on is not None:
            self._amp_power_misses = 0
        if on:
            # Whatever stopped the last wake has evidently been fixed.
            self._wake_failed_at = None
        return on

    async def _async_read_amp_power_patiently(self) -> bool | None:
        """Amplifier power, asked up to three times: one late reply is routine."""
        for attempt in range(AMP_POWER_READ_ATTEMPTS):
            if attempt:
                await asyncio.sleep(AMP_POWER_READ_INTERVAL)
            on = await self._async_read_amp_power()
            if on is not None:
                return on
        return None

    async def _async_read_group_power(
        self, op: _PowerOp | None = None
    ) -> dict[int, bool] | None:
        """The status page's zone flags, or None if it is not answering.

        Bounded by the confirm timeout rather than the HTTP client's own, and
        given up on for the rest of an operation after one failure: the page
        answers in well under a tenth of a second when it answers at all, and
        every read here happens with the command lock held.
        """
        if op is not None and op.http_failed:
            return None
        try:
            async with asyncio.timeout(POWER_CONFIRM_TIMEOUT):
                power = await self.http.group_power()
        except (SonanceHttpError, TimeoutError):
            if op is not None:
                op.http_failed = True
            return None
        self._group_power_at = self._now()
        return power

    async def _async_await_group_power(
        self, group: int, *, on: bool, op: _PowerOp
    ) -> tuple[dict[int, bool] | None, bool]:
        """Wait for the status page to show a zone's new power state.

        Returns the latest group-power map and whether it confirmed the change.
        The map is None when the status page is unreachable, in which case the
        echo is all there is -- and each power write's echo is checked against
        the command and the group it was for.

        Never resends the command itself. The caller does, because a zone-on
        has to be followed by the mute restore every time it is sent.
        """
        deadline = self._now() + POWER_CONFIRM_TIMEOUT
        while True:
            power = await self._async_read_group_power(op)
            if power is None:
                return None, False
            if power.get(group) is on:
                return power, True
            if self._now() >= deadline:
                return power, False
            await asyncio.sleep(POWER_CONFIRM_INTERVAL)

    def _check_open(self) -> None:
        if self._closing:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="not_connected"
            )

    @staticmethod
    def _log_retry(group: int, *, on: bool) -> None:
        _LOGGER.debug(
            "Zone %s did not report %s; sending the command once more",
            GROUP_LETTERS[group],
            "on" if on else "off",
        )

    @staticmethod
    def _raise_not_confirmed(group: int, *, on: bool) -> NoReturn:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="zone_power_not_confirmed",
            translation_placeholders={
                "zone": GROUP_LETTERS[group],
                "state": "on" if on else "off",
            },
        )

    def _apply_power(
        self,
        group: int,
        *,
        zone_on: bool,
        amp_on: bool | None,
        power: dict[int, bool] | None,
        **fields: object,
    ) -> None:
        """Push the new power state to entities without waiting for a poll."""
        if self.data is None:
            return
        group_power = dict(power) if power is not None else dict(self.data.group_power)
        group_power[group] = zone_on
        current = self.data.groups.get(group)
        groups = dict(self.data.groups)
        if current is not None and fields:
            groups[group] = replace(
                current, **{k: v for k, v in fields.items() if v is not None}
            )
        self._generation += 1
        self.async_set_updated_data(
            replace(
                self.data,
                groups=groups,
                group_power=group_power,
                amp_power=amp_on if amp_on is not None else self.data.amp_power,
            )
        )

    def _publish_power(
        self,
        known_off: set[int],
        *,
        amp_on: bool,
        group_power: dict[int, bool] | None = None,
        group: int | None = None,
        muted: bool | object | None = _UNCHANGED,
    ) -> None:
        """Push power state that is not one zone switching cleanly on or off.

        ``muted`` is written as given for ``group`` -- None included, meaning
        unknown -- unless it is left as ``_UNCHANGED``.
        """
        if self.data is None:
            return
        power = (
            dict(group_power)
            if group_power is not None
            else dict(self.data.group_power)
        )
        for zone in known_off:
            power[zone] = False
        groups = dict(self.data.groups)
        if group is not None and muted is not _UNCHANGED and group in groups:
            groups[group] = replace(groups[group], muted=muted)
        self._generation += 1
        self.async_set_updated_data(
            replace(self.data, groups=groups, group_power=power, amp_power=amp_on)
        )

    async def async_close(self) -> None:
        """Release the amplifier's single control session.

        Waits, within a bound, for a power change in progress: cutting one off
        mid-wake leaves the zones the wake revived playing. Anything still
        queued behind it is refused rather than started.
        """
        self._closing = True
        try:
            async with asyncio.timeout(CLOSE_WAIT), self.command_lock:
                pass
        except TimeoutError:
            _LOGGER.warning("A power change was still running at unload")
        await self.client.disconnect()

"""Media player platform for the Sonance DSP integration.

One entity per populated output group. Source selection, power and the
amp-level entity are deliberately absent: this declares only the features it
actually implements, because a declared-but-unimplemented feature is a broken
control on somebody's dashboard.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    CONF_MAX_DB,
    DEFAULT_MAX_DB,
    GROUP_LETTERS,
    MAX_VOLUME_DB,
    MIN_VOLUME_DB,
)
from .coordinator import SonanceConfigEntry, SonanceCoordinator
from .entity import SonanceEntity, command
from .protocol import GroupState

# One socket, one command at a time. A coordinator centralises inbound polling
# but does nothing to limit outbound service calls.
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SonanceConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one media player per discovered group."""
    coordinator = entry.runtime_data
    raw_max = entry.options.get(CONF_MAX_DB, DEFAULT_MAX_DB)
    # Options come back from a NumberSelector as a float, and a user-supplied
    # ceiling below the floor would invert the whole mapping.
    max_db = max(MIN_VOLUME_DB + 1, min(MAX_VOLUME_DB, int(raw_max)))
    async_add_entities(
        SonanceZone(coordinator, group, max_db) for group in coordinator.groups
    )


class SonanceZone(SonanceEntity, MediaPlayerEntity):
    """One output group of the amplifier."""

    # RECEIVER is load-bearing, and it is a trade rather than a free win.
    #
    # HomeKit Bridge routes media_player by device class. RECEIVER reaches the
    # receiver accessory, which is the only route that carries a real volume
    # characteristic -- and it builds its speaker service only when VOLUME_MUTE
    # or VOLUME_STEP is present, so VOLUME_SET alone would yield nothing there.
    #
    # The cost: HA treats TV/RECEIVER/PROJECTOR as accessory-mode-only, so a
    # bridge created through the UI excludes these zones silently. Exposing them
    # to HomeKit means a separate HomeKit instance per zone -- the same thing
    # every AVR integration requires. With SPEAKER they would bridge, but as a
    # mute-only switch with no volume at all, which is the wrong half.
    _attr_device_class = MediaPlayerDeviceClass.RECEIVER
    _attr_supported_features = (
        MediaPlayerEntityFeature.VOLUME_SET
        | MediaPlayerEntityFeature.VOLUME_STEP
        | MediaPlayerEntityFeature.VOLUME_MUTE
    )

    def __init__(
        self, coordinator: SonanceCoordinator, group: int, max_db: int
    ) -> None:
        super().__init__(coordinator)
        self._group = group
        self._max_db = max_db
        letter = GROUP_LETTERS[group]
        self._attr_unique_id = f"{coordinator.identity.serial}_{letter.lower()}"
        self._attr_name = coordinator.group_name(group) or f"Zone {letter}"
        # One device dB per step, rather than Home Assistant's default 10%.
        self._attr_volume_step = 1 / self._span

    # --- volume mapping ----------------------------------------------------

    @property
    def _span(self) -> int:
        """dB between silence and the configured ceiling. Never zero."""
        return max(1, self._max_db - MIN_VOLUME_DB)

    def _to_level(self, db: int) -> float:
        return max(0.0, min(1.0, (db - MIN_VOLUME_DB) / self._span))

    def _to_db(self, level: float) -> int:
        return round(MIN_VOLUME_DB + max(0.0, min(1.0, level)) * self._span)

    # --- state -------------------------------------------------------------

    @property
    def _state(self) -> GroupState | None:
        return self.coordinator.data.groups.get(self._group)

    @property
    def state(self) -> MediaPlayerState | None:
        """ON when the zone answered this cycle, unknown otherwise.

        Group power is deliberately NOT used here. The amplifier has no
        group-power query over TCP and no TURN_ON is declared, so rendering a
        zone OFF would strip its volume and mute controls and leave no way to
        turn it back on -- a dead tile. Power lands with the feature that can
        act on it.
        """
        state = self._state
        if state is None or not state.answered:
            return None
        return MediaPlayerState.ON

    @property
    def volume_level(self) -> float | None:
        state = self._state
        if state is None or state.volume_db is None:
            return None
        return self._to_level(state.volume_db)

    @property
    def is_volume_muted(self) -> bool | None:
        state = self._state
        return None if state is None else state.muted

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state = self._state
        attrs: dict[str, Any] = {
            "group": GROUP_LETTERS[self._group],
            "max_volume_db": self._max_db,
        }
        if state is not None and state.volume_db is not None:
            # dB is what the installer and the amplifier both speak, and a
            # 0-100% slider cannot express it.
            attrs["volume_db"] = state.volume_db
        if state is not None and state.source_name:
            attrs["source_name"] = state.source_name
        power = self.coordinator.data.group_power.get(self._group)
        if power is not None:
            attrs["group_power"] = "on" if power else "off"
        return attrs

    # --- commands ----------------------------------------------------------
    #
    # Each write applies the new value to the cached state immediately rather
    # than awaiting a refresh. Awaiting one would block the whole platform for
    # the length of a poll (PARALLEL_UPDATES = 1), and the coordinator's
    # refresh debounce makes a dragged slider visibly snap back to its old
    # value first. The next scheduled poll reconciles.

    @command
    async def async_set_volume_level(self, volume: float) -> None:
        db = self._to_db(volume)
        await self.coordinator.client.set_volume(self._group, db)
        self.coordinator.apply_optimistic(self._group, volume_db=db)

    @command
    async def async_volume_up(self) -> None:
        await self._step(+1)

    @command
    async def async_volume_down(self) -> None:
        await self._step(-1)

    async def _step(self, delta: int) -> None:
        """Move one device dB, respecting the configured ceiling and floor.

        Uses an absolute set when the current level is known, so the ceiling is
        enforced by the same mapping as the slider. Falls back to the device's
        own relative command when it is not -- better a step against an unknown
        baseline than no response to a button press.
        """
        state = self._state
        if state is None or state.volume_db is None:
            client = self.coordinator.client
            if delta > 0:
                await client.volume_up(self._group)
            else:
                await client.volume_down(self._group)
            return
        target = max(MIN_VOLUME_DB, min(self._max_db, state.volume_db + delta))
        if target == state.volume_db:
            return
        await self.coordinator.client.set_volume(self._group, target)
        self.coordinator.apply_optimistic(self._group, volume_db=target)

    @command
    async def async_mute_volume(self, mute: bool) -> None:
        await self.coordinator.client.set_mute(self._group, mute)
        self.coordinator.apply_optimistic(self._group, muted=mute)

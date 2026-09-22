"""Media player platform for the Sonance DSP integration.

One entity per populated output group, with volume, mute and source selection.

Zones can also mirror metadata from an upstream player. The amplifier knows it
is amplifying line input 1; it has no idea that a streamer on the other end of
that cable is playing a particular track. Linking the two is what turns a tile
reading "On, 61%" into one that shows what is actually playing.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.const import STATE_PAUSED, STATE_PLAYING
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event

from .const import (
    CONF_INPUT_LINKS,
    CONF_MAX_DB,
    DEFAULT_MAX_DB,
    DOMAIN,
    GROUP_LETTERS,
    MAX_VOLUME_DB,
    MIN_VOLUME_DB,
    SOURCE_COUNT,
)
from .coordinator import SonanceConfigEntry, SonanceCoordinator
from .entity import SonanceEntity, command
from .protocol import GroupState

# One socket, one command at a time. A coordinator centralises inbound polling
# but does nothing to limit outbound service calls.
PARALLEL_UPDATES = 1

# Attributes proxied verbatim from a linked upstream player.
_LINKED_ATTRS = (
    "media_title",
    "media_artist",
    "media_album_name",
    "media_content_id",
    "media_content_type",
    "media_duration",
    "media_position",
    "media_position_updated_at",
    "entity_picture",
)


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
    links: dict[str, str] = entry.options.get(CONF_INPUT_LINKS) or {}
    async_add_entities(
        SonanceZone(coordinator, group, max_db, links)
        for group in coordinator.groups
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
    # every AVR integration requires.
    _attr_device_class = MediaPlayerDeviceClass.RECEIVER
    _attr_supported_features = (
        MediaPlayerEntityFeature.VOLUME_SET
        | MediaPlayerEntityFeature.VOLUME_STEP
        | MediaPlayerEntityFeature.VOLUME_MUTE
        | MediaPlayerEntityFeature.SELECT_SOURCE
    )

    def __init__(
        self,
        coordinator: SonanceCoordinator,
        group: int,
        max_db: int,
        input_links: dict[str, str],
    ) -> None:
        super().__init__(coordinator)
        self._group = group
        self._max_db = max_db
        self._input_links = input_links
        self._linked_entity_id: str | None = None
        self._unsub_link: Callable[[], None] | None = None
        letter = GROUP_LETTERS[group]
        self._attr_unique_id = f"{coordinator.identity.serial}_{letter.lower()}"
        self._attr_name = coordinator.group_name(group) or f"Zone {letter}"
        # One device dB per step, rather than Home Assistant's default 10%.
        self._attr_volume_step = 1 / self._span

    # --- volume mapping ----------------------------------------------------

    @property
    def _span(self) -> int:
        """dB between silence and the configured ceiling. Never zero."""
        return max(1, self._effective_max_db - MIN_VOLUME_DB)

    @property
    def _effective_max_db(self) -> int:
        """The configured ceiling, capped by the amplifier's own.

        The amplifier keeps a per-channel maximum of its own, and a group is
        only as loud as its most restricted channel. Promising a user 0 dB on
        a zone the installer capped lower would be a slider that stops short
        with no explanation.
        """
        device_max = self.coordinator.maximum_db(self._group)
        if device_max is None:
            return self._max_db
        return min(self._max_db, device_max)

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
        """ON when the zone answered, or the linked player's transport state.

        Group power is deliberately not used: the amplifier has no group-power
        query over TCP and no TURN_ON is declared, so rendering a zone OFF would
        strip its controls and leave no way to turn it back on.
        """
        state = self._state
        if state is None or not state.answered:
            return None
        upstream = self._linked_state()
        if upstream is not None:
            if upstream.state == STATE_PLAYING:
                return MediaPlayerState.PLAYING
            if upstream.state == STATE_PAUSED:
                return MediaPlayerState.PAUSED
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

    # --- source ------------------------------------------------------------

    @property
    def source_list(self) -> list[str]:
        """The amplifier's four inputs, by their installer-assigned names.

        Inputs are stereo pairs and the amplifier reports the LEFT member's
        name in a source query, so the left name is what is offered here. That
        keeps ``source`` and ``source_list`` trivially consistent instead of
        inventing a label the device never uses.
        """
        names = self.coordinator.input_names()
        return [names[i * 2] for i in range(SOURCE_COUNT) if i * 2 < len(names)]

    @property
    def source(self) -> str | None:
        state = self._state
        return None if state is None else state.source_name

    @command
    async def async_select_source(self, source: str) -> None:
        number = self.coordinator.source_number_for_input_name(source)
        if number is None:
            # ServiceValidationError rather than a bare ValueError: this is bad
            # input, not a fault, and HA renders it to the user instead of
            # logging a traceback. Also reached when the channel layout could
            # not be read at all, which is why the message says both.
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_source",
                translation_placeholders={
                    "source": source,
                    "sources": ", ".join(self.source_list) or "none",
                },
            )
        await self.coordinator.client.set_source(self._group, number)
        self.coordinator.apply_optimistic(self._group, source_name=source)

    # --- linked upstream player -------------------------------------------

    def _linked_target(self) -> str | None:
        """The entity linked to the source this zone is currently playing."""
        state = self._state
        if state is None or state.source_name is None:
            return None
        number = self.coordinator.source_number_for_input_name(state.source_name)
        if number is None:
            return None
        return self._input_links.get(str(number))

    def _linked_state(self):
        if self._linked_entity_id is None:
            return None
        return self.hass.states.get(self._linked_entity_id)

    def _linked_attr(self, name: str) -> Any:
        upstream = self._linked_state()
        return None if upstream is None else upstream.attributes.get(name)

    @property
    def media_title(self) -> str | None:
        return self._linked_attr("media_title")

    @property
    def media_artist(self) -> str | None:
        return self._linked_attr("media_artist")

    @property
    def media_album_name(self) -> str | None:
        return self._linked_attr("media_album_name")

    @property
    def media_content_id(self) -> str | None:
        return self._linked_attr("media_content_id")

    @property
    def media_content_type(self) -> str | None:
        return self._linked_attr("media_content_type")

    @property
    def media_duration(self) -> int | None:
        return self._linked_attr("media_duration")

    @property
    def media_position(self) -> int | None:
        return self._linked_attr("media_position")

    @property
    def media_position_updated_at(self) -> datetime | None:
        return self._linked_attr("media_position_updated_at")

    @property
    def entity_picture(self) -> str | None:
        return self._linked_attr("entity_picture")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._resubscribe()

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_link is not None:
            self._unsub_link()
            self._unsub_link = None
        await super().async_will_remove_from_hass()

    @callback
    def _handle_coordinator_update(self) -> None:
        # The link follows the ROUTE, so selecting a different source has to
        # move the subscription with it.
        self._resubscribe()
        super()._handle_coordinator_update()

    @callback
    def _resubscribe(self) -> None:
        target = self._linked_target()
        if target == self._linked_entity_id:
            return
        if self._unsub_link is not None:
            self._unsub_link()
            self._unsub_link = None
        self._linked_entity_id = target
        if target is not None:
            self._unsub_link = async_track_state_change_event(
                self.hass, [target], self._linked_changed
            )

    @callback
    def _linked_changed(self, _event: Event[EventStateChangedData]) -> None:
        self.async_write_ha_state()

    # --- attributes --------------------------------------------------------

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state = self._state
        attrs: dict[str, Any] = {
            "group": GROUP_LETTERS[self._group],
            "max_volume_db": self._effective_max_db,
        }
        if self._effective_max_db != self._max_db:
            # The amplifier's own ceiling is lower than the configured one, so
            # say so rather than letting the slider quietly stop short.
            attrs["max_volume_db_capped_by_device"] = True
        if state is not None and state.volume_db is not None:
            # dB is what the installer and the amplifier both speak, and a
            # 0-100% slider cannot express it.
            attrs["volume_db"] = state.volume_db
        gain = self.coordinator.gain_offset(self._group)
        if gain is not None:
            # Installer calibration. Two zones at the same dB with different
            # offsets are not at the same loudness, so the figure above is not
            # comparable between zones without this.
            attrs["gain_offset_db"] = gain
        if self._linked_entity_id is not None:
            attrs["linked_entity_id"] = self._linked_entity_id
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
        target = max(
            MIN_VOLUME_DB, min(self._effective_max_db, state.volume_db + delta)
        )
        if target == state.volume_db:
            return
        await self.coordinator.client.set_volume(self._group, target)
        self.coordinator.apply_optimistic(self._group, volume_db=target)

    @command
    async def async_mute_volume(self, mute: bool) -> None:
        await self.coordinator.client.set_mute(self._group, mute)
        self.coordinator.apply_optimistic(self._group, muted=mute)

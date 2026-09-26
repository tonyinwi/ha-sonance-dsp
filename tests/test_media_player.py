"""Tests for the zone media_player entities.

Covers the two things added alongside source selection that are easy to get
subtly wrong and invisible when they are:

* the source NAME is what identifies a source, because the TCP reply's digit is
  a fixed label that never changes
* the upstream link follows the ROUTE, so selecting a different source has to
  move the subscription with it
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sonance_dsp.const import DOMAIN
from custom_components.sonance_dsp.coordinator import SonanceCoordinator, SonanceData
from custom_components.sonance_dsp.http_api import AmplifierIdentity, Topology
from custom_components.sonance_dsp.media_player import SonanceZone
from custom_components.sonance_dsp.protocol import GroupState

IDENTITY = AmplifierIdentity(
    serial="SERIAL123", name="Back Yard", model="DSP8-130 MKII", firmware="V2.2.8130"
)

TOPOLOGY = Topology(
    output_names=[
        "Patio L", "Patio R", "Deck L", "Deck R",
        "Sub L", "Sub R", "Output 4L", "Output 4R",
    ],
    input_names=[
        "Streamer L Digital", "Streamer R Digital",
        "Input 2L", "Input 2R",
        "Streamer L Analog", "Streamer R Analog",
        "Input 4L", "Input 4R",
    ],
    output_groups=["a", "a", "b", "b", "c", "c", "d", "d"],
    maximum_volumes=["12", "12", "-10", "-10", "12", "12", "12", "12"],
    gain_offset=["-6", "-6", "-1", "-1", "4", "4", "0", "0"],
)


@pytest.fixture
async def coordinator(hass: HomeAssistant) -> SonanceCoordinator:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=IDENTITY.serial, data={})
    entry.add_to_hass(hass)
    client = MagicMock()
    client.set_source = AsyncMock()
    client.set_volume = AsyncMock()
    client.set_mute = AsyncMock()
    client.volume_up = AsyncMock()
    client.volume_down = AsyncMock()
    c = SonanceCoordinator(hass, entry, client, IDENTITY, "192.0.2.10")
    c._topology = TOPOLOGY
    c.groups = [0, 1, 2, 3]
    c.data = SonanceData(
        identity=IDENTITY,
        topology=TOPOLOGY,
        groups={
            g: GroupState(
                group=g,
                volume_db=-27,
                muted=False,
                source_name="Streamer L Digital",
            )
            for g in (0, 1, 2, 3)
        },
        group_power={},
    )
    return c


def zone(
    coordinator: SonanceCoordinator,
    hass: HomeAssistant,
    group: int = 0,
    max_db: int = 0,
    links: dict[str, str] | None = None,
) -> SonanceZone:
    z = SonanceZone(coordinator, group, max_db, links or {})
    z.hass = hass
    z.entity_id = f"media_player.zone_{group}"
    return z


# ---------------------------------------------------------------------------
# Identity and features
# ---------------------------------------------------------------------------


def test_zone_is_a_receiver_with_the_features_it_implements(
    coordinator: SonanceCoordinator, hass: HomeAssistant
) -> None:
    z = zone(coordinator, hass)
    assert z._attr_device_class is MediaPlayerDeviceClass.RECEIVER
    f = z.supported_features
    for flag in (
        MediaPlayerEntityFeature.VOLUME_SET,
        MediaPlayerEntityFeature.VOLUME_STEP,
        MediaPlayerEntityFeature.VOLUME_MUTE,
        MediaPlayerEntityFeature.SELECT_SOURCE,
    ):
        assert flag in f
    # Not declared, because not implemented -- a declared-but-missing feature
    # is a broken control on someone's dashboard.
    assert MediaPlayerEntityFeature.TURN_ON not in f
    assert MediaPlayerEntityFeature.PLAY not in f


def test_zone_name_comes_from_the_device(
    coordinator: SonanceCoordinator, hass: HomeAssistant
) -> None:
    assert zone(coordinator, hass, 0)._attr_name == "Patio"
    assert zone(coordinator, hass, 1)._attr_name == "Deck"


# ---------------------------------------------------------------------------
# Volume, and the amplifier's own ceiling
# ---------------------------------------------------------------------------


def test_volume_maps_onto_the_configured_ceiling(
    coordinator: SonanceCoordinator, hass: HomeAssistant
) -> None:
    z = zone(coordinator, hass, 0, max_db=0)
    assert z.volume_level == pytest.approx((-27 + 70) / 70)
    assert z._to_db(1.0) == 0
    assert z._to_db(0.0) == -70


def test_device_ceiling_wins_when_it_is_lower(
    coordinator: SonanceCoordinator, hass: HomeAssistant
) -> None:
    """Group B's channels are capped at -10 dB by the amplifier itself.

    Promising 0 dB on a zone the installer capped lower would be a slider that
    stops short with no explanation.
    """
    z = zone(coordinator, hass, 1, max_db=0)
    assert z._effective_max_db == -10
    assert z._to_db(1.0) == -10
    assert z.extra_state_attributes["max_volume_db"] == -10
    assert z.extra_state_attributes["max_volume_db_capped_by_device"] is True


def test_no_cap_flag_when_the_configured_ceiling_is_lower(
    coordinator: SonanceCoordinator, hass: HomeAssistant
) -> None:
    z = zone(coordinator, hass, 0, max_db=-20)
    assert z._effective_max_db == -20
    assert "max_volume_db_capped_by_device" not in z.extra_state_attributes


def test_gain_offset_is_surfaced_because_db_is_not_comparable(
    coordinator: SonanceCoordinator, hass: HomeAssistant
) -> None:
    """Two zones at -27 dB with offsets -6 and +4 are ten dB apart."""
    assert zone(coordinator, hass, 0).extra_state_attributes["gain_offset_db"] == -6
    assert zone(coordinator, hass, 2).extra_state_attributes["gain_offset_db"] == 4


# ---------------------------------------------------------------------------
# Source selection
# ---------------------------------------------------------------------------


def test_source_list_offers_the_left_input_of_each_pair(
    coordinator: SonanceCoordinator, hass: HomeAssistant
) -> None:
    """Matching what the amplifier reports keeps source and source_list consistent."""
    assert zone(coordinator, hass).source_list == [
        "Streamer L Digital",
        "Input 2L",
        "Streamer L Analog",
        "Input 4L",
    ]


def test_current_source_is_the_name_the_device_reported(
    coordinator: SonanceCoordinator, hass: HomeAssistant
) -> None:
    assert zone(coordinator, hass).source == "Streamer L Digital"


async def test_select_source_resolves_the_name_to_a_number(
    coordinator: SonanceCoordinator, hass: HomeAssistant
) -> None:
    """The name is the only reliable identifier.

    The TCP reply's ``Src1=`` digit stays 1 whatever is selected, so a client
    that trusted the digit would send source 1 every time.
    """
    z = zone(coordinator, hass)
    await z.async_select_source("Streamer L Analog")
    coordinator.client.set_source.assert_awaited_once_with(0, 3)
    assert z.source == "Streamer L Analog"


async def test_select_unknown_source_raises_before_the_socket(
    coordinator: SonanceCoordinator, hass: HomeAssistant
) -> None:
    z = zone(coordinator, hass)
    with pytest.raises(ServiceValidationError):
        await z.async_select_source("Not A Real Input")
    coordinator.client.set_source.assert_not_awaited()


# ---------------------------------------------------------------------------
# Upstream mirroring
# ---------------------------------------------------------------------------


def test_no_link_means_no_metadata(
    coordinator: SonanceCoordinator, hass: HomeAssistant
) -> None:
    z = zone(coordinator, hass)
    z._resubscribe()
    assert z.media_title is None
    assert z.entity_picture is None
    assert z.state is MediaPlayerState.ON
    assert "linked_entity_id" not in z.extra_state_attributes


def test_linked_player_metadata_is_proxied(
    coordinator: SonanceCoordinator, hass: HomeAssistant
) -> None:
    hass.states.async_set(
        "media_player.streamer",
        "playing",
        {
            "media_title": "Fourth of July",
            "media_artist": "Sufjan Stevens",
            "media_album_name": "Carrie & Lowell",
            "media_duration": 292,
            "entity_picture": "/api/media_player_proxy/streamer",
        },
    )
    z = zone(coordinator, hass, links={"1": "media_player.streamer"})
    z._resubscribe()

    assert z.media_title == "Fourth of July"
    assert z.media_artist == "Sufjan Stevens"
    assert z.media_album_name == "Carrie & Lowell"
    assert z.media_duration == 292
    assert z.entity_picture == "/api/media_player_proxy/streamer"
    assert z.state is MediaPlayerState.PLAYING
    assert z.extra_state_attributes["linked_entity_id"] == "media_player.streamer"


def test_link_follows_the_route_not_the_zone(
    coordinator: SonanceCoordinator, hass: HomeAssistant
) -> None:
    """Selecting a different source must move the subscription with it.

    A link is attached to a SOURCE. A zone routed away from that source must
    stop showing its metadata, or it reports a track that is no longer audible
    in that room.
    """
    hass.states.async_set("media_player.streamer", "playing", {"media_title": "A"})
    hass.states.async_set("media_player.turntable", "playing", {"media_title": "B"})
    links = {"1": "media_player.streamer", "3": "media_player.turntable"}

    z = zone(coordinator, hass, links=links)
    z._resubscribe()
    assert z.media_title == "A"

    coordinator.data.groups[0] = GroupState(
        group=0, volume_db=-27, muted=False, source_name="Streamer L Analog",
    )
    z._resubscribe()
    assert z.media_title == "B"

    # And a source with no link shows nothing rather than the stale track.
    coordinator.data.groups[0] = GroupState(
        group=0, volume_db=-27, muted=False, source_name="Input 2L",
    )
    z._resubscribe()
    assert z.media_title is None


def test_paused_upstream_shows_paused(
    coordinator: SonanceCoordinator, hass: HomeAssistant
) -> None:
    hass.states.async_set("media_player.streamer", "paused", {})
    z = zone(coordinator, hass, links={"1": "media_player.streamer"})
    z._resubscribe()
    assert z.state is MediaPlayerState.PAUSED


def test_idle_upstream_leaves_the_zone_simply_on(
    coordinator: SonanceCoordinator, hass: HomeAssistant
) -> None:
    """The zone is still amplifying whatever is on the wire."""
    hass.states.async_set("media_player.streamer", "idle", {})
    z = zone(coordinator, hass, links={"1": "media_player.streamer"})
    z._resubscribe()
    assert z.state is MediaPlayerState.ON


def test_zone_that_did_not_answer_has_no_state(
    coordinator: SonanceCoordinator, hass: HomeAssistant
) -> None:
    """None, not ON. A dataclass with empty fields is still truthy."""
    coordinator.data.groups[0] = GroupState(group=0)
    z = zone(coordinator, hass)
    assert z.state is None
    assert z.volume_level is None

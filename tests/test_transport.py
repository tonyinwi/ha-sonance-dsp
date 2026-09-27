"""Transport pass-through, driven through Home Assistant's service layer.

The unit tests in test_media_player call the zone's methods directly, so they
never see what sits between a service call and the handler: the platform's
PARALLEL_UPDATES semaphore, the feature check, and other integrations' players
calling back. These tests set up real zones on the real platform, with a real
upstream player from another platform.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerState,
)
from homeassistant.components.media_player import (
    MediaPlayerEntityFeature as F,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceNotSupported
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import intent
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockModule,
    MockPlatform,
    mock_integration,
    mock_platform,
)

from custom_components.sonance_dsp.const import CONF_INPUT_LINKS, DOMAIN
from custom_components.sonance_dsp.coordinator import SonanceCoordinator, SonanceData
from custom_components.sonance_dsp.protocol import GroupState

from .test_media_player import IDENTITY, TOPOLOGY

PATIO = "media_player.back_yard_patio"
DECK = "media_player.back_yard_deck"
STREAMER = "media_player.streamer"


class FakePlayer(MediaPlayerEntity):
    _attr_should_poll = False
    _attr_supported_features = (
        F.PLAY | F.PAUSE | F.STOP | F.NEXT_TRACK | F.PREVIOUS_TRACK
    )

    def __init__(self) -> None:
        self._attr_name = "streamer"
        self._attr_unique_id = "streamer"
        self._attr_state = MediaPlayerState.PAUSED
        self.calls: list[str] = []
        self.delay = 0.0

    async def _did(self, call: str, state: MediaPlayerState | None = None) -> None:
        self.calls.append(call)
        await asyncio.sleep(self.delay)
        if state is not None:
            self._attr_state = state
        self.async_write_ha_state()

    async def async_media_play(self) -> None:
        await self._did("play", MediaPlayerState.PLAYING)

    async def async_media_pause(self) -> None:
        await self._did("pause", MediaPlayerState.PAUSED)

    async def async_media_stop(self) -> None:
        await self._did("stop", MediaPlayerState.IDLE)

    async def async_media_next_track(self) -> None:
        await self._did("next")


async def setup_zones(
    hass: HomeAssistant, link: str = STREAMER, players: list[dict] | None = None
) -> tuple[FakePlayer, SonanceCoordinator]:
    """Patio and Deck on the streamer's input, both on; plus any other players."""
    streamer = FakePlayer()

    async def _add(hass, config, async_add_entities, discovery_info=None):
        async_add_entities([streamer])

    mock_integration(hass, MockModule("fakeplayer"))
    mock_platform(
        hass, "fakeplayer.media_player", MockPlatform(async_setup_platform=_add)
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=IDENTITY.serial,
        data={},
        options={CONF_INPUT_LINKS: {"1": link}},
    )
    entry.add_to_hass(hass)
    client = MagicMock()
    for method in ("set_source", "set_volume", "set_mute", "disconnect"):
        setattr(client, method, AsyncMock())
    c = SonanceCoordinator(hass, entry, client, IDENTITY, "192.0.2.10")
    c.update_interval = None
    c._topology = TOPOLOGY
    c.groups = [0, 1]
    c.data = SonanceData(
        identity=IDENTITY,
        topology=TOPOLOGY,
        groups={
            g: GroupState(
                group=g, volume_db=-27, muted=False, source_name="Streamer L Digital"
            )
            for g in (0, 1)
        },
        group_power={0: True, 1: True},
        amp_power=True,
    )
    entry.runtime_data = c
    assert await async_setup_component(
        hass,
        "media_player",
        {"media_player": [{"platform": "fakeplayer"}, *(players or [])]},
    )
    entry.mock_state(hass, ConfigEntryState.LOADED)
    await hass.config_entries.async_forward_entry_setups(entry, ["media_player"])
    await hass.async_block_till_done()
    assert hass.states.get(PATIO) is not None
    assert hass.states.get(DECK) is not None
    return streamer, c


async def call(hass: HomeAssistant, service: str, entity_id, **data) -> None:
    """Call a media_player service, failing rather than hanging."""
    async with asyncio.timeout(2):
        await hass.services.async_call(
            "media_player", service, {"entity_id": entity_id, **data}, blocking=True
        )


async def test_play_through_the_service_layer(hass: HomeAssistant) -> None:
    streamer, _ = await setup_zones(hass)

    await call(hass, "media_play", PATIO)

    assert streamer.calls == ["play"]


async def test_an_upstream_calling_back_into_a_zone_does_not_hang(
    hass: HomeAssistant,
) -> None:
    """A universal player whose play unmutes the other zone, then plays.

    With PARALLEL_UPDATES = 1 the zone forwarding play holds the platform's one
    semaphore, so the unmute waits for it forever, and so does every zone.
    """
    assert await async_setup_component(
        hass,
        "script",
        {
            "script": {
                "deck_play": {
                    "sequence": [
                        {
                            "action": "media_player.volume_mute",
                            "target": {"entity_id": DECK},
                            "data": {"is_volume_muted": False},
                        },
                        {
                            "action": "media_player.media_play",
                            "target": {"entity_id": STREAMER},
                        },
                    ]
                }
            }
        },
    )
    streamer, c = await setup_zones(
        hass,
        link="media_player.patio_combined",
        players=[
            {
                "platform": "universal",
                "name": "Patio Combined",
                "children": [STREAMER],
                "commands": {"media_play": {"action": "script.deck_play"}},
            }
        ],
    )

    await call(hass, "media_play", PATIO)

    assert streamer.calls == ["play"]
    c.client.set_mute.assert_awaited_once_with(1, False)


async def test_a_slow_upstream_does_not_hold_up_other_zones(
    hass: HomeAssistant,
) -> None:
    streamer, c = await setup_zones(hass)
    c.async_turn_off = AsyncMock()  # the amp dialogue is not the point here
    streamer.delay = 5
    play = hass.async_create_task(
        hass.services.async_call(
            "media_player", "media_play", {"entity_id": PATIO}, blocking=True
        )
    )
    await asyncio.sleep(0.05)
    assert streamer.calls == ["play"]

    await call(hass, "volume_mute", DECK, is_volume_muted=True)
    await call(hass, "turn_off", DECK)

    assert not play.done()
    play.cancel()
    await asyncio.gather(play, return_exceptions=True)
    c.client.set_mute.assert_awaited_once_with(1, True)
    c.async_turn_off.assert_awaited_once_with(1)


@pytest.mark.parametrize(
    ("service", "expected"),
    [("media_play", "play"), ("media_pause", "pause"), ("media_stop", "stop")],
)
async def test_a_forward_that_comes_back_is_dropped(
    hass: HomeAssistant, service: str, expected: str
) -> None:
    """A zone linked to a group containing itself: the group sends it back.

    Unchecked, it goes round for as long as the zone is on, about a thousand
    calls a second.
    """
    streamer, c = await setup_zones(
        hass,
        link="media_player.patio_audio",
        players=[
            {"platform": "group", "name": "Patio Audio", "entities": [STREAMER, PATIO]}
        ],
    )
    assert hass.states.get(PATIO).attributes["supported_features"] & F.PLAY

    await call(hass, service, PATIO)
    for _ in range(20):
        await asyncio.sleep(0.01)
    calls = list(streamer.calls)
    # Break any loop before asserting, so a regression fails rather than hangs.
    c.data.group_power[0] = False
    c.async_set_updated_data(c.data)
    await hass.async_block_till_done()

    assert calls == [expected]


async def test_one_next_track_skips_once(hass: HomeAssistant) -> None:
    """Assist targets every playing player that can skip: not the zones."""
    from homeassistant.components.media_player import intent as media_intent

    streamer, _ = await setup_zones(hass)
    await async_setup_component(hass, "homeassistant", {})
    await media_intent.async_setup_intents(hass)
    area = ar.async_get(hass).async_create("Patio")
    for entity_id in (STREAMER, PATIO, DECK):
        er.async_get(hass).async_update_entity(entity_id, area_id=area.id)
    await call(hass, "media_play", STREAMER)
    streamer.calls.clear()

    await intent.async_handle(
        hass, "test", "HassMediaNext", {"area": {"value": "Patio"}}
    )
    await hass.async_block_till_done()

    assert streamer.calls == ["next"]


async def test_play_on_an_off_zone_is_not_offered(hass: HomeAssistant) -> None:
    streamer, c = await setup_zones(hass)
    c.data.group_power[0] = False
    c.async_set_updated_data(c.data)
    await hass.async_block_till_done()

    with pytest.raises(ServiceNotSupported):
        await call(hass, "media_play", PATIO)

    assert streamer.calls == []

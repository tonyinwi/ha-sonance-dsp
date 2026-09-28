"""Play and browse media, through the real service layer and websocket.

Patio and Deck are on input 1, linked to the streamer; Sub is on input 2,
linked to a second player. All three zones are on. The players are shaped like
Music Assistant's: play_media reads ``extra`` and the caller's user.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.components.media_player import (
    BrowseError,
    BrowseMedia,
    MediaClass,
    MediaPlayerEntity,
    MediaPlayerState,
    SearchMedia,
    SearchMediaQuery,
)
from homeassistant.components.media_player import MediaPlayerEntityFeature as F
from homeassistant.components.media_player import intent as media_intent
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Context, HomeAssistant, State
from homeassistant.exceptions import ServiceNotSupported, ServiceValidationError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import intent
from homeassistant.helpers.state import async_reproduce_state
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
SUB = "media_player.back_yard_sub"
STREAMER = "media_player.streamer"
OTHER = "media_player.other"
MEDIA = (
    F.PLAY_MEDIA | F.BROWSE_MEDIA | F.SEARCH_MEDIA | F.MEDIA_ENQUEUE | F.MEDIA_ANNOUNCE
)
OFFERED = F.PLAY_MEDIA | F.BROWSE_MEDIA | F.MEDIA_ENQUEUE


class MediaPlayer(MediaPlayerEntity):
    """Shaped like Music Assistant's: reads extra and the caller's user."""

    _attr_should_poll = False
    _attr_supported_features = F.PLAY | F.PAUSE | F.STOP | F.NEXT_TRACK | MEDIA

    def __init__(self, name: str) -> None:
        self._attr_name = name
        self._attr_unique_id = name
        self._attr_state = MediaPlayerState.IDLE
        self.calls: list[Any] = []
        self.contexts: list[Context | None] = []
        self.suspends = True
        self.delay = 0.0
        self.browse_raises = False

    async def _wait(self) -> None:
        if self.delay:
            await asyncio.sleep(self.delay)
        elif self.suspends:
            await asyncio.sleep(0)

    async def async_media_play(self) -> None:
        self.calls.append("play")
        await self._wait()
        self._attr_state = MediaPlayerState.PLAYING
        self.async_write_ha_state()

    async def async_media_pause(self) -> None:
        self.calls.append("pause")
        await self._wait()
        self._attr_state = MediaPlayerState.PAUSED
        self.async_write_ha_state()

    async def async_play_media(self, media_type, media_id, **kwargs) -> None:
        kwargs["extra"]  # KeyError without it, as on Music Assistant
        user = self._context.user_id if self._context else None
        self.calls.append(("play_media", media_type, media_id, kwargs, user))
        self.contexts.append(self._context)
        await self._wait()
        self._attr_state = MediaPlayerState.PLAYING
        self._attr_media_content_id = media_id
        self._attr_media_content_type = media_type
        self.async_write_ha_state()

    async def async_browse_media(self, media_content_type=None, media_content_id=None):
        self.calls.append(("browse", media_content_type, media_content_id))
        if self.browse_raises:
            raise NotImplementedError
        return BrowseMedia(
            media_class=MediaClass.DIRECTORY,
            media_content_id="root",
            media_content_type="library",
            title="Library",
            can_play=False,
            can_expand=True,
            children=[
                BrowseMedia(
                    media_class=MediaClass.TRACK,
                    media_content_id="library://track/1",
                    media_content_type="track",
                    title="One",
                    can_play=True,
                    can_expand=False,
                    thumbnail=self.get_browse_image_url("track", "library://track/1"),
                )
            ],
        )

    async def async_get_browse_image(
        self, media_content_type, media_content_id, media_image_id=None
    ):
        return b"img", "image/png"

    async def async_search_media(self, query: SearchMediaQuery) -> SearchMedia:
        self.calls.append(("search", query.search_query))
        root = await self.async_browse_media()
        return SearchMedia(result=list(root.children))


async def setup(
    hass: HomeAssistant,
    link: str = STREAMER,
    players: list[dict] | None = None,
    muted: dict[int, bool | None] | None = None,
) -> tuple[MediaPlayer, MediaPlayer, SonanceCoordinator]:
    streamer, other = MediaPlayer("streamer"), MediaPlayer("other")

    async def _add(hass, config, async_add_entities, discovery_info=None):
        async_add_entities([streamer, other])

    mock_integration(hass, MockModule("fakeplayer"))
    mock_platform(
        hass, "fakeplayer.media_player", MockPlatform(async_setup_platform=_add)
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=IDENTITY.serial,
        data={},
        options={CONF_INPUT_LINKS: {"1": link, "2": OTHER}},
    )
    entry.add_to_hass(hass)
    client = MagicMock()
    for m in ("set_source", "set_volume", "set_mute", "disconnect"):
        setattr(client, m, AsyncMock())
    c = SonanceCoordinator(hass, entry, client, IDENTITY, "192.0.2.10")
    c.update_interval = None
    c._topology = TOPOLOGY
    c.groups = [0, 1, 2]
    muted = muted or {}
    c.data = SonanceData(
        identity=IDENTITY,
        topology=TOPOLOGY,
        groups={
            g: GroupState(
                group=g,
                volume_db=-27,
                muted=muted.get(g, False),
                source_name="Input 2L" if g == 2 else "Streamer L Digital",
            )
            for g in (0, 1, 2)
        },
        group_power={0: True, 1: True, 2: True},
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
    assert hass.states.get(SUB) is not None
    return streamer, other, c


async def call(hass, service, entity_id=None, context=None, target=None, **data):
    async with asyncio.timeout(2):
        return await hass.services.async_call(
            "media_player",
            service,
            {**({"entity_id": entity_id} if entity_id else {}), **data},
            blocking=True,
            context=context,
            target=target,
            return_response=service in ("browse_media", "search_media"),
        )


async def play(hass, entity_id=None, **data):
    data = {"media_content_type": "track", "media_content_id": "x", **data}
    return await call(hass, "play_media", entity_id, **data)


async def set_power(hass, c, group: int, on: bool | None) -> None:
    if on is None:
        c.data.group_power.pop(group, None)
    else:
        c.data.group_power[group] = on
    c.async_set_updated_data(c.data)
    await hass.async_block_till_done()


def features(hass, entity_id) -> F:
    return F(hass.states.get(entity_id).attributes["supported_features"])


def plays(player: MediaPlayer) -> list:
    return [x for x in player.calls if isinstance(x, tuple) and x[0] == "play_media"]


def browses(player: MediaPlayer) -> list:
    return [x for x in player.calls if isinstance(x, tuple) and x[0] == "browse"]


def in_area(hass, *entity_ids) -> str:
    area = ar.async_get(hass).async_create("Back Yard")
    for entity_id in entity_ids:
        er.async_get(hass).async_update_entity(entity_id, area_id=area.id)
    return area.id


# --- features ---------------------------------------------------------------


async def test_an_on_zone_offers_play_browse_and_enqueue_only(
    hass: HomeAssistant,
) -> None:
    await setup(hass)

    assert features(hass, PATIO) & MEDIA == OFFERED


@pytest.mark.parametrize("power", [False, None], ids=["off", "unknown"])
async def test_a_zone_not_known_to_be_on_offers_no_media(
    hass: HomeAssistant, power: bool | None
) -> None:
    _, _, c = await setup(hass)
    await set_power(hass, c, 0, power)

    assert not features(hass, PATIO) & MEDIA


async def test_muting_does_not_change_the_features(hass: HomeAssistant) -> None:
    """Features follow power only: each change reloads HomeKit accessories."""
    streamer, _, _ = await setup(hass, muted={0: True})

    assert features(hass, PATIO) == features(hass, DECK)
    # Muted is not off: the source plays, this zone stays silent.
    await play(hass, PATIO)
    assert len(plays(streamer)) == 1


# --- play_media --------------------------------------------------------------


async def test_play_media_goes_to_the_player_as_the_caller(
    hass: HomeAssistant, hass_admin_user
) -> None:
    streamer, _, _ = await setup(hass)
    context = Context(user_id=hass_admin_user.id)

    await play(
        hass,
        PATIO,
        context=context,
        media_content_id="library://track/1",
        enqueue="replace",
        extra={"radio_mode": True},
    )

    [(_, kind, media_id, kwargs, user)] = plays(streamer)
    # Music Assistant plays as this user: it reads the forwarded context.
    assert (kind, media_id, user) == ("track", "library://track/1", hass_admin_user.id)
    assert kwargs == {"enqueue": "replace", "extra": {"radio_mode": True}}
    assert streamer.contexts[0].parent_id == context.id


async def test_media_source_ids_reach_the_player_unresolved(
    hass: HomeAssistant,
) -> None:
    """The player resolves them against itself."""
    streamer, _, _ = await setup(hass)

    await play(hass, PATIO, media_content_id="media-source://media_source/local/a.mp3")

    assert plays(streamer)[0][2] == "media-source://media_source/local/a.mp3"


async def test_the_zone_then_shows_it_playing_without_the_media_id(
    hass: HomeAssistant,
) -> None:
    await setup(hass)

    await play(hass, PATIO)
    await hass.async_block_till_done()

    state = hass.states.get(PATIO)
    assert state.state == "playing"
    assert state.attributes["media_content_type"] == "track"
    # Kept off the zone: a scene would replay it (see the scene tests).
    assert "media_content_id" not in state.attributes


@pytest.mark.parametrize(
    "media_id", ["http://x/a.mp3", "media-source://tts/cloud?message=hi"]
)
async def test_announcements_are_refused(hass: HomeAssistant, media_id: str) -> None:
    streamer, _, _ = await setup(hass)

    with pytest.raises(ServiceValidationError) as err:
        await play(hass, PATIO, media_content_id=media_id, announce=True)
    await play(hass, PATIO, announce=False)

    assert err.value.translation_key == "announce_refused"
    assert len(plays(streamer)) == 1
    assert not plays(streamer)[0][3].get("announce")


async def test_what_tts_speak_sends_is_refused(hass: HomeAssistant) -> None:
    """The call tts/entity.py makes: a list of players, announce=True, blocking."""
    streamer, _, _ = await setup(hass)

    with pytest.raises(ServiceValidationError):
        await play(
            hass,
            [PATIO, DECK],
            media_content_type="music",
            media_content_id="media-source://tts/demo?message=hi",
            announce=True,
        )

    assert plays(streamer) == []


async def test_a_group_of_zones_passes_on_no_announcement(
    hass: HomeAssistant,
) -> None:
    """A group forwards without blocking, so the refusal is only logged."""
    streamer, _, _ = await setup(
        hass, players=[{"platform": "group", "name": "Yard", "entities": [PATIO, DECK]}]
    )

    assert not features(hass, "media_player.yard") & F.MEDIA_ANNOUNCE
    await play(hass, "media_player.yard", announce=True)
    await hass.async_block_till_done()

    assert plays(streamer) == []


async def test_play_media_on_an_off_zone(hass: HomeAssistant) -> None:
    streamer, _, c = await setup(hass)
    await set_power(hass, c, 0, False)

    with pytest.raises(ServiceNotSupported):
        await play(hass, PATIO)
    # Named with a zone that is on, the call still fails as a whole.
    with pytest.raises(ServiceNotSupported):
        await play(hass, [DECK, PATIO])
    assert plays(streamer) == []
    # By area, the off zone is skipped and the other plays.
    await play(hass, target={"area_id": in_area(hass, PATIO, DECK)})
    assert len(plays(streamer)) == 1


# --- one call, one forward ------------------------------------------------------


@pytest.mark.parametrize("enqueue", [None, "add", "next", "play", "replace"])
@pytest.mark.parametrize("how", ["list", "area", "group"])
async def test_one_call_to_zones_on_a_source_plays_once(
    hass: HomeAssistant, how: str, enqueue: str | None
) -> None:
    streamer, _, _ = await setup(
        hass, players=[{"platform": "group", "name": "Yard", "entities": [PATIO, DECK]}]
    )
    data = {"enqueue": enqueue} if enqueue else {}

    if how == "list":
        await play(hass, [PATIO, DECK], **data)
    elif how == "area":
        await play(hass, target={"area_id": in_area(hass, PATIO, DECK)}, **data)
    else:
        await play(hass, "media_player.yard", **data)
        await hass.async_block_till_done()

    assert len(plays(streamer)) == 1


@pytest.mark.parametrize("suspends", [True, False])
async def test_one_call_plays_and_pauses_once_however_fast_the_player(
    hass: HomeAssistant, suspends: bool
) -> None:
    streamer, _, _ = await setup(hass)
    streamer.suspends = suspends

    await play(hass, [PATIO, DECK], enqueue="add")
    await call(hass, "media_pause", [PATIO, DECK])

    assert len(plays(streamer)) == 1
    assert streamer.calls.count("pause") == 1


async def test_separate_calls_at_once_each_play(hass: HomeAssistant) -> None:
    streamer, _, _ = await setup(hass)

    await asyncio.gather(play(hass, PATIO), play(hass, DECK))

    assert len(plays(streamer)) == 2


async def test_different_requests_in_one_context_each_play(
    hass: HomeAssistant,
) -> None:
    streamer, _, _ = await setup(hass)
    context = Context()

    await asyncio.gather(
        play(hass, PATIO, context=context, enqueue="add"),
        play(hass, DECK, context=context, enqueue="next"),
    )

    assert len(plays(streamer)) == 2


async def test_the_same_play_twice_in_a_script_is_sent_twice(
    hass: HomeAssistant,
) -> None:
    """One call, one forward: not one context, one forward."""
    streamer, _, _ = await setup(hass)
    step = {
        "action": "media_player.play_media",
        "target": {"entity_id": PATIO},
        "data": {"media_content_type": "track", "media_content_id": "x"},
    }
    assert await async_setup_component(
        hass, "script", {"script": {"twice": {"sequence": [step, step]}}}
    )

    await hass.services.async_call("script", "twice", blocking=True)

    assert len(plays(streamer)) == 2


async def test_an_area_with_the_player_and_its_zones_plays_twice(
    hass: HomeAssistant,
) -> None:
    """The known limit: the player's own call cannot be told from the zones'."""
    streamer, _, _ = await setup(hass)

    await play(hass, target={"area_id": in_area(hass, STREAMER, PATIO, DECK)})

    assert len(plays(streamer)) == 2


async def test_zones_on_different_sources_each_play(hass: HomeAssistant) -> None:
    streamer, other, _ = await setup(hass)

    await play(hass, [PATIO, SUB])

    assert len(plays(streamer)) == 1
    assert len(plays(other)) == 1


# --- loops and concurrency --------------------------------------------------------


async def test_play_media_that_comes_back_is_dropped(hass: HomeAssistant) -> None:
    streamer, _, c = await setup(
        hass,
        link="media_player.patio_audio",
        players=[
            {"platform": "group", "name": "Patio Audio", "entities": [STREAMER, PATIO]}
        ],
    )

    await play(hass, PATIO)
    for _ in range(20):
        await asyncio.sleep(0.01)
    n = len(plays(streamer))
    # Break any loop before asserting, so a regression fails rather than hangs.
    await set_power(hass, c, 0, False)

    assert n == 1


async def test_an_automation_on_the_players_state_can_still_pause_the_zone(
    hass: HomeAssistant,
) -> None:
    """No call-path guard on transport: a dropped pause would fail open."""
    streamer, _, _ = await setup(hass)
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "triggers": {
                    "trigger": "state",
                    "entity_id": STREAMER,
                    "to": "playing",
                },
                "actions": {
                    "action": "media_player.media_pause",
                    "target": {"entity_id": PATIO},
                },
            }
        },
    )

    await play(hass, PATIO)
    await hass.async_block_till_done()

    assert [x if isinstance(x, str) else x[0] for x in streamer.calls] == [
        "play_media",
        "pause",
    ]


async def test_a_slow_play_does_not_hold_up_other_zones(hass: HomeAssistant) -> None:
    streamer, _, c = await setup(hass)
    streamer.delay = 5
    task = hass.async_create_task(play(hass, PATIO))
    await asyncio.sleep(0.05)
    assert len(plays(streamer)) == 1

    async with asyncio.timeout(1):
        await call(hass, "volume_mute", DECK, is_volume_muted=True)

    c.client.set_mute.assert_awaited_once_with(1, True)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


# --- browse ------------------------------------------------------------------


async def test_browse_over_the_websocket_is_the_players_own(
    hass: HomeAssistant, hass_ws_client, hass_client_no_auth
) -> None:
    streamer, _, _ = await setup(hass)
    ws = await hass_ws_client(hass)

    await ws.send_json(
        {
            "id": 1,
            "type": "media_player/browse_media",
            "entity_id": PATIO,
            "media_content_type": "library",
            "media_content_id": "root",
        }
    )
    msg = await ws.receive_json()

    assert msg["success"], msg
    assert browses(streamer) == [("browse", "library", "root")]
    [child] = msg["result"]["children"]
    assert child["media_content_id"] == "library://track/1"
    # Thumbnails stay on the player's own proxy, and load.
    assert child["thumbnail"].startswith(f"/api/media_player_proxy/{STREAMER}/")
    resp = await (await hass_client_no_auth()).get(child["thumbnail"])
    assert resp.status == 200


async def test_the_browse_service_works_on_a_muted_zone(hass: HomeAssistant) -> None:
    await setup(hass, muted={0: True})

    resp = await call(hass, "browse_media", PATIO)

    assert isinstance(resp[PATIO], BrowseMedia)


async def test_browse_an_off_zone_is_refused_by_service_and_websocket(
    hass: HomeAssistant, hass_ws_client
) -> None:
    streamer, _, c = await setup(hass)
    await set_power(hass, c, 0, False)
    ws = await hass_ws_client(hass)

    # The service has no feature gate: the zone refuses by itself.
    with pytest.raises(BrowseError) as err:
        await call(hass, "browse_media", PATIO)
    await ws.send_json(
        {"id": 1, "type": "media_player/browse_media", "entity_id": PATIO}
    )
    msg = await ws.receive_json()

    assert err.value.translation_key == "browse_unavailable"
    assert not msg["success"]
    assert msg["error"]["code"] == "not_supported"
    assert browses(streamer) == []


async def test_a_player_that_browses_back_into_the_zone_is_refused(
    hass: HomeAssistant, hass_ws_client
) -> None:
    """Browse carries no context: a Context-based check cannot see this loop."""
    await setup(
        hass,
        link="media_player.patio_combined",
        players=[
            {
                "platform": "universal",
                "name": "Patio Combined",
                "children": [STREAMER],
                "browse_media_entity": PATIO,
            }
        ],
    )
    ws = await hass_ws_client(hass)

    await ws.send_json(
        {"id": 1, "type": "media_player/browse_media", "entity_id": PATIO}
    )
    msg = await ws.receive_json()

    # How HA reports a BrowseError; a RecursionError would read the same code.
    assert msg["error"] == {"code": "unknown_error", "message": "browse_unavailable"}
    with pytest.raises(BrowseError):
        await call(hass, "browse_media", PATIO)


async def test_a_player_that_cannot_browse_is_not_reported_as_our_bug(
    hass: HomeAssistant, hass_ws_client, caplog: pytest.LogCaptureFixture
) -> None:
    streamer, _, _ = await setup(hass)
    streamer.browse_raises = True
    ws = await hass_ws_client(hass)
    caplog.set_level(logging.ERROR)

    await ws.send_json(
        {"id": 1, "type": "media_player/browse_media", "entity_id": PATIO}
    )
    msg = await ws.receive_json()

    assert msg["error"]["code"] == "unknown_error"
    assert "allows media browsing but its integration" not in caplog.text


async def test_search_is_not_offered(hass: HomeAssistant) -> None:
    await setup(hass)

    with pytest.raises(ServiceNotSupported):
        await call(hass, "search_media", PATIO, search_query="one")


# --- scenes and Assist -----------------------------------------------------------


@pytest.mark.parametrize(
    ("captured", "expected"), [("playing", ["play"]), ("paused", ["pause"])]
)
async def test_a_scene_resumes_or_pauses_and_never_replays(
    hass: HomeAssistant, captured: str, expected: list
) -> None:
    streamer, _, c = await setup(hass)
    c.async_turn_on = AsyncMock()
    await play(hass, PATIO, media_content_id="old")
    await hass.async_block_till_done()
    snapshot = hass.states.get(PATIO)
    streamer.calls.clear()

    # Volume and mute are restored too, and are not the point here.
    attrs = {
        k: v
        for k, v in snapshot.attributes.items()
        if k not in ("volume_level", "is_volume_muted")
    }
    await async_reproduce_state(hass, [State(PATIO, captured, attrs)])
    await hass.async_block_till_done()

    assert streamer.calls == expected


async def test_a_scene_saved_with_a_media_id_replays_it(hass: HomeAssistant) -> None:
    """The known limit: scenes captured before the id stopped being mirrored."""
    streamer, _, c = await setup(hass)
    c.async_turn_on = AsyncMock()

    await async_reproduce_state(
        hass,
        [
            State(
                PATIO,
                "paused",
                {"media_content_type": "track", "media_content_id": "old"},
            )
        ],
    )
    await hass.async_block_till_done()

    assert [x if isinstance(x, str) else x[:3] for x in streamer.calls] == [
        ("play_media", "track", "old"),
        "pause",
    ]


async def test_assist_search_and_play_picks_the_player_not_a_zone(
    hass: HomeAssistant,
) -> None:
    streamer, _, _ = await setup(hass)
    await async_setup_component(hass, "homeassistant", {})
    await media_intent.async_setup_intents(hass)
    in_area(hass, STREAMER, PATIO, DECK)

    await intent.async_handle(
        hass,
        "test",
        "HassMediaSearchAndPlay",
        {"search_query": {"value": "one"}, "area": {"value": "Back Yard"}},
    )
    await hass.async_block_till_done()

    assert ("search", "one") in streamer.calls
    assert len(plays(streamer)) == 1

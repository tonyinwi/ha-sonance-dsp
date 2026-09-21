"""Tests for the amplifier's read-only HTTP JSON client.

The endpoint under test is undocumented -- it was found by reading the web UI's
own JavaScript -- so these tests are written against payloads shaped like the
ones captured from a live DSP 8-130 MKII, not against a vendor schema.

Two things are load-bearing here and are asserted rather than assumed:

* **Every request is ``action=read``.** An ``action=write`` form exists on the
  same handler and this integration must never reach it (see ``docs/design.md``
  -- the write path was observed accepting a request it did not apply).
* **Losing the endpoint is not fatal.** Each failure mode has to arrive as a
  ``SonanceHttpError``, because the coordinator catches exactly that to keep
  volume control working when group power is unavailable.

Mocking: ``AiohttpClientMocker`` from pytest-homeassistant-custom-component.
``create_session()`` hands back a real ``ClientSession`` with ``_request``
swapped out, which is the right seam for this code -- ``SonanceHttpApi`` is
handed a session rather than fetching one from hass, so no HomeAssistant
instance is needed, and the mocker records every URL for the read-only check.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncGenerator

import aiohttp
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.sonance_dsp.http_api import (
    AmplifierIdentity,
    SonanceHttpApi,
    SonanceHttpError,
    Topology,
    _strip_channel_suffix,
)

HOST = "192.0.2.10"

# Page matchers. The mocker matches a compiled pattern against the whole URL,
# which keeps these independent of the cache-buster and parameter order.
GENERAL = re.compile(r"page=general-settings")
STATUS = re.compile(r"page=status")
IN_OUT = re.compile(r"page=in-out-settings")
ANY_PAGE = re.compile(r"Handler\.php")

# Shaped like a real general-settings reply: identity fields sit alongside
# network config, and the amp answers with strings throughout.
GENERAL_SETTINGS_PAYLOAD = {
    "serial-number": "SDA8130M2-1194827",
    "amplifier-name": "Garden Amp",
    "amplifier-model": "DSP 8-130 MKII",
    "firmware-version": "V2.2.8130",
    "dhcp-enable": "on",
    "ip-address": "192.0.2.10",
    "subnet-mask": "255.255.255.0",
    "gateway": "192.0.2.1",
    "mac-address": "00:1E:C0:AA:BB:CC",
    "tcp-port": "52000",
}

STATUS_PAYLOAD = {
    "status-titles": [f"GROUP {letter}" for letter in "ABCDEFGH"],
    "power-status": ["on", "on", "off", "on", "off", "off", "off", "off"],
    "mute-volumes": ["off", "on", "off", "off", "off", "off", "off", "off"],
}

IN_OUT_PAYLOAD = {
    "output-names": [
        "Patio L",
        "Patio R",
        "Deck L",
        "Deck R",
        "Kitchen L",
        "Kitchen R",
        "Office L",
        "Office R",
    ],
    "input-names": [
        "Streamer L Digital",
        "Streamer R Digital",
        "Input 2L",
        "Input 2R",
    ],
    "output-groups": ["a", "a", "b", "b", "c", "c", "d", "d"],
    "dsp-presets": [44, 44, 47, 47, 48, 48, 0, 0],
    # Fields the older "basicsettings" page does not return at all. Their
    # absence is exactly why the endpoint was switched.
    "turn-on-volumes": ["-27", "-27", "-27", "-27", "-27", "-27", "-45", "-45"],
    "maximum-volumes": ["12", "12", "12", "12", "0", "0", "12", "12"],
    "gain-offset": ["-6", "-6", "-1", "-1", "4", "4", "0", "0"],
    "level-trim-dBs": ["0", "0", "0", "0", "0", "0", "0", "0"],
    "stereo-or-mono": ["stereo"] * 8,
    "mode-sources": ["off"] * 8,
    "sources-1": [0, 1, 0, 1, 0, 1, 0, 1],
    "sources-2": [4, 5, 4, 5, 4, 5, 6, 7],
    "output-volumes": ["-27", "-27", "-31", "-31", "-20", "-20", "-70", "-70"],
}


@pytest.fixture
def amp() -> AiohttpClientMocker:
    """A stand-in amplifier web server. Register pages on it per test."""
    return AiohttpClientMocker()


@pytest.fixture
async def api(amp: AiohttpClientMocker) -> AsyncGenerator[SonanceHttpApi]:
    """The client under test, bound to the fake amplifier."""
    session = amp.create_session(asyncio.get_running_loop())
    try:
        yield SonanceHttpApi(session, HOST)
    finally:
        await session.close()


def topology(**overrides: list[str]) -> Topology:
    """Build a Topology directly -- the parsing path is tested separately."""
    fields: dict[str, list[str]] = {
        "output_names": [],
        "input_names": [],
        "output_groups": [],
    }
    fields.update(overrides)
    return Topology(**fields)  # type: ignore[arg-type]


# --- identity --------------------------------------------------------------


async def test_identity_parses_general_settings(
    amp: AiohttpClientMocker, api: SonanceHttpApi
) -> None:
    """Serial, name, model and firmware come off the general-settings page."""
    amp.get(GENERAL, json=GENERAL_SETTINGS_PAYLOAD)

    assert await api.identity() == AmplifierIdentity(
        serial="SDA8130M2-1194827",
        name="Garden Amp",
        model="DSP 8-130 MKII",
        firmware="V2.2.8130",
    )


async def test_identity_strips_whitespace(
    amp: AiohttpClientMocker, api: SonanceHttpApi
) -> None:
    """The amp pads some fields; the serial becomes a unique_id, so it must not."""
    amp.get(
        GENERAL,
        json={**GENERAL_SETTINGS_PAYLOAD, "serial-number": "  SDA8130M2-1194827  "},
    )

    assert (await api.identity()).serial == "SDA8130M2-1194827"


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(
            {k: v for k, v in GENERAL_SETTINGS_PAYLOAD.items() if k != "serial-number"},
            id="key-absent",
        ),
        pytest.param({**GENERAL_SETTINGS_PAYLOAD, "serial-number": ""}, id="empty"),
        pytest.param(
            {**GENERAL_SETTINGS_PAYLOAD, "serial-number": "   "}, id="whitespace-only"
        ),
        pytest.param({}, id="page-empty"),
    ],
)
async def test_identity_without_serial_raises(
    amp: AiohttpClientMocker, api: SonanceHttpApi, payload: dict
) -> None:
    """No serial means no stable unique_id, so setup must fail loudly.

    These amps are typically on DHCP, so falling back to the IP would key the
    config entry on something that moves.
    """
    amp.get(GENERAL, json=payload)

    with pytest.raises(SonanceHttpError, match="serial"):
        await api.identity()


async def test_identity_falls_back_when_name_and_model_blank(
    amp: AiohttpClientMocker, api: SonanceHttpApi
) -> None:
    """A never-commissioned amp reports blank name/model; DeviceInfo still needs one."""
    amp.get(
        GENERAL,
        json={
            "serial-number": "SDA8130M2-1194827",
            "amplifier-name": "",
            "amplifier-model": "   ",
        },
    )

    identity = await api.identity()

    assert identity.name == "Sonance DSP"
    assert identity.model == "Sonance DSP"
    assert identity.firmware == ""


# --- group power and mute --------------------------------------------------


async def test_group_power_maps_on_off_to_bools(
    amp: AiohttpClientMocker, api: SonanceHttpApi
) -> None:
    """This page is the *only* source of per-group power -- TCP has no query opcode."""
    amp.get(STATUS, json={"power-status": ["on", "off", "on"]})

    assert await api.group_power() == {0: True, 1: False, 2: True}


async def test_group_power_full_eight_groups(
    amp: AiohttpClientMocker, api: SonanceHttpApi
) -> None:
    """Index is the 0-based group number, A=0 ... H=7."""
    amp.get(STATUS, json=STATUS_PAYLOAD)

    assert await api.group_power() == {
        0: True,
        1: True,
        2: False,
        3: True,
        4: False,
        5: False,
        6: False,
        7: False,
    }


@pytest.mark.parametrize("value", ["ON", "On", "oN"])
async def test_group_power_is_case_insensitive(
    amp: AiohttpClientMocker, api: SonanceHttpApi, value: str
) -> None:
    """Casing on this undocumented endpoint is not contractual."""
    amp.get(STATUS, json={"power-status": [value]})

    assert await api.group_power() == {0: True}


@pytest.mark.parametrize(
    "payload", [{}, {"power-status": []}, {"power-status": None}], ids=str
)
async def test_group_power_absent_returns_empty(
    amp: AiohttpClientMocker, api: SonanceHttpApi, payload: dict
) -> None:
    """A missing key is degraded state, not an error: the zones stay controllable."""
    amp.get(STATUS, json=payload)

    assert await api.group_power() == {}


async def test_group_mute_maps_on_off_to_bools(
    amp: AiohttpClientMocker, api: SonanceHttpApi
) -> None:
    amp.get(STATUS, json={"mute-volumes": ["on", "off", "on"]})

    assert await api.group_mute() == {0: True, 1: False, 2: True}


async def test_group_mute_reads_the_mute_key_not_the_power_key(
    amp: AiohttpClientMocker, api: SonanceHttpApi
) -> None:
    """Both live on the same page; crossing them would be invisible in normal use."""
    amp.get(STATUS, json=STATUS_PAYLOAD)

    assert await api.group_mute() == {
        0: False,
        1: True,
        2: False,
        3: False,
        4: False,
        5: False,
        6: False,
        7: False,
    }


# --- topology --------------------------------------------------------------


async def test_topology_parses_in_out_settings(
    amp: AiohttpClientMocker, api: SonanceHttpApi
) -> None:
    amp.get(IN_OUT, json=IN_OUT_PAYLOAD)

    result = await api.topology()

    assert result.output_names == IN_OUT_PAYLOAD["output-names"]
    assert result.input_names == IN_OUT_PAYLOAD["input-names"]
    assert result.output_groups == ["a", "a", "b", "b", "c", "c", "d", "d"]


async def test_topology_strips_and_stringifies(
    amp: AiohttpClientMocker, api: SonanceHttpApi
) -> None:
    """Installer-typed names arrive padded; group letters must compare cleanly."""
    amp.get(
        IN_OUT,
        json={
            "output-names": ["  Patio L  ", "Patio R"],
            "input-names": [" Streamer L Digital "],
            "output-groups": [" a ", "a"],
        },
    )

    result = await api.topology()

    assert result.output_names == ["Patio L", "Patio R"]
    assert result.input_names == ["Streamer L Digital"]
    assert result.output_groups == ["a", "a"]
    assert result.group_name(0) == "Patio"


@pytest.mark.parametrize("payload", [{}, {"output-names": None}], ids=str)
async def test_topology_tolerates_missing_keys(
    amp: AiohttpClientMocker, api: SonanceHttpApi, payload: dict
) -> None:
    amp.get(IN_OUT, json=payload)

    result = await api.topology()

    assert result.output_names == []
    assert result.input_names == []
    assert result.output_groups == []


# --- Topology.group_name ---------------------------------------------------


def test_group_name_strips_lr_suffix() -> None:
    """``Patio L`` + ``Patio R`` -> ``Patio``. This is how zones get their names."""
    topo = topology(
        output_names=["Patio L", "Patio R", "Deck L", "Deck R"],
        output_groups=["a", "a", "b", "b"],
    )

    assert topo.group_name(0) == "Patio"
    assert topo.group_name(1) == "Deck"


def test_group_name_strips_long_suffix_form() -> None:
    topo = topology(
        output_names=["Study Left", "Study Right"], output_groups=["a", "a"]
    )

    assert topo.group_name(0) == "Study"


def test_group_name_accepts_uppercase_group_letters() -> None:
    """``output-groups`` casing is undocumented; both forms must map to group A."""
    topo = topology(output_names=["Patio L", "Patio R"], output_groups=["A", "A"])

    assert topo.group_name(0) == "Patio"


def test_group_name_is_none_for_a_group_with_no_members() -> None:
    """An unpopulated group has no zone, and must not produce a phantom entity."""
    topo = topology(
        output_names=["Patio L", "Patio R"],
        output_groups=["a", "a"],
    )

    assert topo.group_name(2) is None  # group C
    assert topo.group_name(7) is None  # group H


def test_group_name_for_a_single_member_group() -> None:
    """A mono zone -- one channel assigned on its own -- still names cleanly."""
    topo = topology(output_names=["Subwoofer L"], output_groups=["a"])

    assert topo.group_name(0) == "Subwoofer"


def test_group_name_ignores_channels_beyond_the_names_list() -> None:
    """``output-groups`` can be longer than ``output-names`` on a partly named amp."""
    topo = topology(output_names=["Patio L"], output_groups=["a", "a"])

    assert topo.group_name(0) == "Patio"


def test_group_name_when_members_share_no_stem() -> None:
    """Mismatched member names: the code returns the FIRST member's full name.

    Note what this is *not*: it is not a common prefix, and it is not the
    stripped form. ``Sub L`` and ``Array R`` strip to ``Sub`` and ``Array``,
    which are unequal, so the fallback returns ``members[0]`` untouched --
    channel marker and all. The zone is therefore called "Sub L".

    Asserted as-is rather than as the behaviour one might prefer; see the
    summary for why it is arguably wrong.
    """
    topo = topology(output_names=["Sub L", "Array R"], output_groups=["a", "a"])

    assert topo.group_name(0) == "Sub L"


def test_group_name_is_none_when_the_only_member_is_unnamed() -> None:
    """A blank name must not become a blank-string zone name."""
    topo = topology(output_names=["", ""], output_groups=["a", "a"])

    assert topo.group_name(0) is None


# --- Topology.group_members ------------------------------------------------


def test_group_members_returns_channel_indices() -> None:
    topo = topology(
        output_names=IN_OUT_PAYLOAD["output-names"],
        output_groups=["a", "a", "b", "b", "c", "c", "d", "d"],
    )

    assert topo.group_members(0) == [0, 1]
    assert topo.group_members(1) == [2, 3]
    assert topo.group_members(2) == [4, 5]
    assert topo.group_members(3) == [6, 7]


def test_group_members_handles_non_contiguous_assignment() -> None:
    """Channels can be assigned to a group in any order via the web UI."""
    topo = topology(output_groups=["a", "b", "b", "a"])

    assert topo.group_members(0) == [0, 3]
    assert topo.group_members(1) == [1, 2]


def test_group_members_is_empty_for_an_unpopulated_group() -> None:
    topo = topology(output_groups=["a", "a"])

    assert topo.group_members(4) == []


# --- failure modes ---------------------------------------------------------


@pytest.mark.parametrize("status", [401, 404, 500, 503])
@pytest.mark.parametrize("call", ["identity", "group_power", "group_mute", "topology"])
async def test_non_200_raises(
    amp: AiohttpClientMocker, api: SonanceHttpApi, status: int, call: str
) -> None:
    """A firmware update could move this undocumented endpoint. That must surface."""
    amp.get(ANY_PAGE, status=status)

    with pytest.raises(SonanceHttpError):
        await getattr(api, call)()


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(TimeoutError(), id="timeout"),
        pytest.param(aiohttp.ClientConnectionError("refused"), id="connection-refused"),
        pytest.param(aiohttp.ClientError("boom"), id="client-error"),
    ],
)
async def test_transport_errors_raise(
    amp: AiohttpClientMocker, api: SonanceHttpApi, exc: Exception
) -> None:
    """The amp being asleep or off must not escape as a raw aiohttp exception.

    The coordinator catches SonanceHttpError specifically, to lose group power
    rather than take every zone entity unavailable.
    """
    amp.get(ANY_PAGE, exc=exc)

    with pytest.raises(SonanceHttpError):
        await api.group_power()


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("<html>404 not found</html>", id="html-error-page"),
        pytest.param("", id="empty-body"),
        pytest.param('{"power-status": ["on",', id="truncated-json"),
    ],
)
async def test_malformed_json_raises(
    amp: AiohttpClientMocker, api: SonanceHttpApi, body: str
) -> None:
    """The amp serves JSON as text/html, so the body is the only thing to trust."""
    amp.get(ANY_PAGE, text=body)

    with pytest.raises(SonanceHttpError):
        await api.group_power()


async def test_error_message_names_the_page(
    amp: AiohttpClientMocker, api: SonanceHttpApi
) -> None:
    """Three pages fail independently; the log line has to say which one."""
    amp.get(ANY_PAGE, status=500)

    with pytest.raises(SonanceHttpError, match="in-out-settings"):
        await api.topology()


# --- read-only guarantee ---------------------------------------------------


async def test_every_request_is_action_read(
    amp: AiohttpClientMocker, api: SonanceHttpApi
) -> None:
    """The whole client must be incapable of reaching ``action=write``.

    ``docs/protocol.md``: the vendor's own HTTP write endpoint was observed
    reporting success for a change it never applied. Everything this
    integration needs to write is writable over TCP, so nothing here has any
    business issuing a write.
    """
    amp.get(GENERAL, json=GENERAL_SETTINGS_PAYLOAD)
    amp.get(STATUS, json=STATUS_PAYLOAD)
    amp.get(IN_OUT, json=IN_OUT_PAYLOAD)

    await api.identity()
    await api.group_power()
    await api.group_mute()
    await api.topology()

    assert amp.call_count == 4
    for method, url, _data, _headers in amp.mock_calls:
        assert method.lower() == "get"
        assert url.query.get("action") == "read"
        assert "action=write" not in str(url)
        assert url.path == "/Web/Handler.php"
        assert url.host == HOST


async def test_pages_requested_are_the_three_documented_ones(
    amp: AiohttpClientMocker, api: SonanceHttpApi
) -> None:
    amp.get(GENERAL, json=GENERAL_SETTINGS_PAYLOAD)
    amp.get(STATUS, json=STATUS_PAYLOAD)
    amp.get(IN_OUT, json=IN_OUT_PAYLOAD)

    await api.identity()
    await api.group_power()
    await api.topology()

    assert [url.query["page"] for _m, url, _d, _h in amp.mock_calls] == [
        "general-settings",
        "status",
        "in-out-settings",
    ]


async def test_custom_http_port_is_honoured(amp: AiohttpClientMocker) -> None:
    """The amp's web UI is not always on 80."""
    session = amp.create_session(asyncio.get_running_loop())
    try:
        amp.get(STATUS, json=STATUS_PAYLOAD)
        await SonanceHttpApi(session, HOST, 8080).group_power()
    finally:
        await session.close()

    _method, url, _data, _headers = amp.mock_calls[0]
    assert url.port == 8080
    assert url.query.get("action") == "read"


# --- channel-suffix naming ---------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        # Installer-assigned: space-separated marker.
        ("Patio L", "Patio"),
        ("Patio R", "Patio"),
        ("Deck Left", "Deck"),
        ("Deck Right", "Deck"),
        ("  Patio L  ", "Patio"),
        # Factory default: marker runs straight onto the channel number.
        ("Output 4L", "Output 4"),
        ("Output 4R", "Output 4"),
        ("Output 1L", "Output 1"),
        # Names that merely END in L or R must be left alone -- this is the
        # reason the digit guard exists.
        ("Pool", "Pool"),
        ("Hall", "Hall"),
        ("Cellar", "Cellar"),
        ("XLR", "XLR"),
        ("Bar", "Bar"),
        # No marker at all.
        ("Kitchen", "Kitchen"),
        ("", ""),
    ],
)
def test_strip_channel_suffix(name: str, expected: str) -> None:
    """Both naming conventions, without eating real names."""
    assert _strip_channel_suffix(name) == expected


def test_group_name_for_factory_default_channels() -> None:
    """A pair of un-renamed channels collapses to a single zone name.

    Before this, group D came through as "Output 4L" -- the left channel's
    name standing in for the whole zone, because the two member names did not
    match after stripping and the code fell back to the first member.
    """
    topology = Topology(
        output_names=[
            "Patio L", "Patio R",
            "Output 2L", "Output 2R",
            "Output 3L", "Output 3R",
            "Output 4L", "Output 4R",
        ],
        input_names=[],
        output_groups=["a", "a", "b", "b", "c", "c", "d", "d"],
    )
    assert topology.group_name(0) == "Patio"
    assert topology.group_name(1) == "Output 2"
    assert topology.group_name(3) == "Output 4"


# ---------------------------------------------------------------------------
# Fields that only the In/Out Settings endpoint returns
#
# The older "basicsettings" page answers happily and omits all of these, which
# is the whole reason for the switch -- a wrong-but-working endpoint is harder
# to notice than a broken one.
# ---------------------------------------------------------------------------


async def test_topology_reads_the_fields_basicsettings_omits(
    amp: AiohttpClientMocker, api: SonanceHttpApi
) -> None:
    amp.get(IN_OUT, json=IN_OUT_PAYLOAD)
    t = await api.topology()

    assert t.turn_on_volumes[6] == "-45"
    assert t.maximum_volumes[0] == "12"
    assert t.gain_offset == ["-6", "-6", "-1", "-1", "4", "4", "0", "0"]
    assert t.level_trim_dbs == ["0"] * 8
    assert t.stereo_or_mono == ["stereo"] * 8
    assert t.mode_sources == ["off"] * 8
    assert t.sources_1 == [0, 1, 0, 1, 0, 1, 0, 1]
    assert t.sources_2 == [4, 5, 4, 5, 4, 5, 6, 7]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Streamer L Digital", 1),   # input index 0 -> pair 1
        ("Streamer R Digital", 1),   # index 1, same pair
        ("Input 2L", 2),
        ("Input 2R", 2),
        ("  input 2l  ", 2),         # whitespace and case tolerated
        ("Not An Input", None),
    ],
)
def test_source_number_for_input_name(name: str, expected: int | None) -> None:
    """The TCP reply's digit is a lie; the NAME is what identifies the source.

    Selecting source 2 still answers ``Src1=Input 2L`` -- the label stays
    ``Src1`` whatever is selected. Resolving through input_names is the only
    way to learn which source is actually live.
    """
    t = Topology(
        output_names=[],
        input_names=IN_OUT_PAYLOAD["input-names"],
        output_groups=[],
    )
    assert t.source_number_for_input_name(name) == expected


def test_maximum_db_takes_the_lowest_in_the_group() -> None:
    """A group is only as loud as its most restricted channel.

    The amplifier keeps its own per-channel ceiling, independent of this
    integration's max_db option. Taking the max of the pair would let a zone be
    driven past a limit the installer set on one of its channels.
    """
    t = Topology(
        output_names=IN_OUT_PAYLOAD["output-names"],
        input_names=IN_OUT_PAYLOAD["input-names"],
        output_groups=IN_OUT_PAYLOAD["output-groups"],
        maximum_volumes=IN_OUT_PAYLOAD["maximum-volumes"],
    )
    assert t.maximum_db(0) == 12          # channels 0,1 -> both 12
    assert t.maximum_db(2) == 0           # channels 4,5 -> both 0
    assert t.maximum_db(7) is None        # group H has no members


def test_topology_defaults_are_empty_not_none() -> None:
    """A field the endpoint omits must not become None and crash a consumer."""
    t = Topology(output_names=[], input_names=[], output_groups=[])
    assert t.gain_offset == []
    assert t.sources_1 == []
    assert t.maximum_db(0) is None

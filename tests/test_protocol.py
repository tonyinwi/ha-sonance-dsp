"""Unit tests for the Sonance DSP wire protocol.

Pure unit tests: no Home Assistant fixtures, no ``hass``, no real socket. Tier 2
of the plan in ``docs/design.md`` -- the *real* client driven over a fake
transport, which is where framing, reply parsing and enumeration actually live.

Everything asserted here is a claim ``docs/protocol.md`` makes about the
hardware, so a failure means either the code or the document is wrong about a
device that is expensive to re-measure. Frames and reply literals are written
out byte for byte rather than rebuilt from the constants under test, because a
test that derives its expectation from the same constant it is checking proves
only that the constant equals itself.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.sonance_dsp import const, protocol
from custom_components.sonance_dsp.const import (
    FORBIDDEN_OPCODES,
    MAX_VOLUME_DB,
    MIN_VOLUME_DB,
    REPLY_LENGTH,
    byte_to_db,
    db_to_byte,
)
from custom_components.sonance_dsp.protocol import (
    ForbiddenOpcodeError,
    SonanceConnectionError,
    SonanceProtocol,
)

# --- reply literals --------------------------------------------------------
# Captured from a live DSP 8-130 MKII (firmware V2.2.8130); see docs/protocol.md.
# The two volume forms differ in whitespace in TWO places -- the padding after
# "Cmd:" and the space before "db" -- and a parser written against either one
# alone silently fails on the other.
VOLUME_QUERY_REPLY = "Cmd:Volume      ,Group:D Vol=-27 db"
VOLUME_ECHO_REPLY = "Cmd:VolumeUP   ,Group:D Vol=-27db"
AMP_POWER_REPLY = "Power status :On"

# Mute and source replies are NOT captured verbatim in docs/protocol.md; these
# follow the documented "Cmd:<name><pad>,Group:<letter> <field>" shape in both
# padding variants. Weaker evidence than the volume literals, and flagged as
# such so nobody mistakes them for measurements.
MUTE_QUERY_REPLY = "Cmd:Mute        ,Group:D Mute=on"
MUTE_ECHO_REPLY = "Cmd:MuteOFF    ,Group:D Mute=off"
SOURCE_QUERY_REPLY = "Cmd:Source      ,Group:D Src2=Streamer L Digital"
SOURCE_ECHO_REPLY = "Cmd:Source2    ,Group:B Src2=Input 2"

# --- frames ----------------------------------------------------------------
# Group D is operand 0x03 (groups are 0-based: A=0x00 ... H=0x07).
FRAME_QUERY_VOLUME_D = b"\xff\x55\x02\x10\x03"
FRAME_QUERY_MUTE_D = b"\xff\x55\x02\x12\x03"
FRAME_QUERY_SOURCE_D = b"\xff\x55\x02\x11\x03"
FRAME_AMP_POWER_QUERY = b"\xff\x55\x01\x70"

NUL_REPLY = b"\x00" * REPLY_LENGTH


def padded(text: str) -> bytes:
    """Render a reply the way the amplifier does: 50 bytes, NUL-padded.

    No terminator, which is why the client must read a fixed width rather than
    a line.
    """
    raw = text.encode("ascii")
    assert len(raw) <= REPLY_LENGTH, f"reply too long for the wire: {text!r}"
    return raw.ljust(REPLY_LENGTH, b"\x00")


def query_volume_frame(group: int) -> bytes:
    """The GET VOLUME frame for a group, as discovery sends it."""
    return b"\xff\x55\x02\x10" + bytes((group,))


class FakeLink:
    """A scripted stand-in for the amplifier's single TCP session.

    The script maps an exact outbound frame to the reply it provokes:

    * ``bytes``            -- answered immediately
    * ``(delay, bytes)``   -- answered after ``delay`` seconds
    * absent from the script -- silence, which is how a group with no channels
      answers, and the case that makes ``readline()`` hang forever

    Replies are fed on write rather than pre-loaded, because pre-loading cannot
    express "this group is silent": a later group's frame would already be in
    the buffer and the silent group would read it.
    """

    def __init__(self, script: dict[bytes, bytes | tuple[float, bytes]]) -> None:
        self._script = script
        self.reader = asyncio.StreamReader()
        self.writer = MagicMock(spec=asyncio.StreamWriter)
        self.writer.write.side_effect = self._on_write
        self.open_connection = AsyncMock(return_value=(self.reader, self.writer))

    def _on_write(self, data: bytes) -> None:
        reply = self._script.get(bytes(data))
        if reply is None:
            return
        if isinstance(reply, tuple):
            delay, payload = reply
            asyncio.get_running_loop().call_later(delay, self.reader.feed_data, payload)
        else:
            self.reader.feed_data(reply)

    @property
    def frames(self) -> list[bytes]:
        """Every frame that reached the socket, in order."""
        return [bytes(call.args[0]) for call in self.writer.write.call_args_list]


def fake_link(script: dict[bytes, bytes | tuple[float, bytes]]):
    """Patch ``asyncio.open_connection`` to hand back the scripted link."""
    link = FakeLink(script)
    return link, patch("asyncio.open_connection", link.open_connection)


# ---------------------------------------------------------------------------
# Framing
# ---------------------------------------------------------------------------


def test_build_frame_global_exact_bytes() -> None:
    """A global frame is FF 55 01 <opcode> -- LEN 01, no operand."""
    frame = SonanceProtocol.build_frame(const.OP_AMP_POWER_QUERY)
    assert frame == FRAME_AMP_POWER_QUERY


def test_build_frame_scoped_exact_bytes() -> None:
    """A scoped frame is FF 55 02 <opcode> <operand>, groups 0-based."""
    assert SonanceProtocol.build_frame(const.OP_QUERY_VOLUME, 0x03) == (
        FRAME_QUERY_VOLUME_D
    )


@pytest.mark.parametrize(
    ("db", "expected"),
    [
        (-40, b"\xff\x55\x02\x8f\x03"),
        (-55, b"\xff\x55\x02\x80\x03"),
        (-70, b"\xff\x55\x02\x71\x03"),
        (-27, b"\xff\x55\x02\x9c\x03"),
    ],
)
def test_build_frame_matches_verified_absolute_volume_sets(
    db: int, expected: bytes
) -> None:
    """The four absolute sets read back exactly on V2.2.8130 (protocol.md)."""
    assert SonanceProtocol.build_frame(db_to_byte(db), 0x03) == expected


@pytest.mark.parametrize("opcode", sorted(range(0x21, 0x29)))
def test_build_frame_refuses_every_forbidden_opcode(opcode: int) -> None:
    """0x21-0x28 reassign channels, and their echo lies. Unreachable by design.

    Checked scoped and global: the operand is what picks the channel, but a
    bare opcode must not slip through either.
    """
    with pytest.raises(ForbiddenOpcodeError):
        SonanceProtocol.build_frame(opcode, 0x0A)
    with pytest.raises(ForbiddenOpcodeError):
        SonanceProtocol.build_frame(opcode)


def test_forbidden_opcodes_covers_exactly_0x21_to_0x28() -> None:
    """Eight opcodes, one per group A-H. Nothing more, nothing less."""
    assert set(FORBIDDEN_OPCODES) == set(range(0x21, 0x29))


async def test_forbidden_opcode_never_reaches_the_socket() -> None:
    """The guard's whole value is being upstream of the wire.

    These opcodes echo success for changes they do not apply, so "send it and
    check afterwards" is not available as a recovery.
    """
    link, patcher = fake_link({})
    client = SonanceProtocol("192.0.2.10", 52000)
    with patcher:
        await client.connect()
        with pytest.raises(ForbiddenOpcodeError):
            await client._request(0x22, 0x0A)
    assert link.frames == []


# ---------------------------------------------------------------------------
# Volume conversion
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("db", "byte"),
    [(-70, 0x71), (-27, 0x9C), (0, 0xB7), (12, 0xC3)],
)
def test_volume_anchor_values(db: int, byte: int) -> None:
    """The four anchors from the device's own table."""
    assert db_to_byte(db) == byte
    assert byte_to_db(byte) == db


def test_db_byte_round_trip_across_whole_range() -> None:
    """byte = dB + 183 over -70..+12 inclusive, and back without drift."""
    for db in range(MIN_VOLUME_DB, MAX_VOLUME_DB + 1):
        value = db_to_byte(db)
        assert 0x00 <= value <= 0xFF, f"{db} dB leaves the byte range"
        assert byte_to_db(value) == db


def test_volume_bytes_never_collide_with_a_forbidden_opcode() -> None:
    """An absolute set puts the volume byte in the OPCODE position.

    So the whole -70..+12 range has to stay clear of 0x21-0x28, or setting a
    volume would reassign a channel. It does (0x71-0xC3), but nothing in the
    code enforces it and the consequence is destructive, so it is asserted.
    """
    used = {db_to_byte(db) for db in range(MIN_VOLUME_DB, MAX_VOLUME_DB + 1)}
    assert used.isdisjoint(FORBIDDEN_OPCODES)


# ---------------------------------------------------------------------------
# Reply parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reply",
    [VOLUME_QUERY_REPLY, VOLUME_ECHO_REPLY],
    ids=["query_form", "command_echo_form"],
)
def test_re_volume_matches_both_literal_forms(reply: str) -> None:
    """Both whitespace forms, same group, same value."""
    match = const.RE_VOLUME.search(reply)
    assert match is not None, f"RE_VOLUME did not match {reply!r}"
    assert match.group(1) == "D"
    assert int(match.group(2)) == -27


def test_re_volume_spans_the_whole_device_range() -> None:
    """One and two digit values, negative and positive, all parse."""
    for db in range(MIN_VOLUME_DB, MAX_VOLUME_DB + 1):
        text = f"Cmd:Volume      ,Group:A Vol={db} db"
        match = const.RE_VOLUME.search(text)
        assert match is not None, f"RE_VOLUME did not match {text!r}"
        assert int(match.group(2)) == db


@pytest.mark.parametrize(
    ("reply", "group", "state"),
    [(MUTE_QUERY_REPLY, "D", "on"), (MUTE_ECHO_REPLY, "D", "off")],
)
def test_re_mute_matches(reply: str, group: str, state: str) -> None:
    match = const.RE_MUTE.search(reply)
    assert match is not None, f"RE_MUTE did not match {reply!r}"
    assert match.groups() == (group, state)


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (SOURCE_QUERY_REPLY, ("D", "2", "Streamer L Digital")),
        (SOURCE_ECHO_REPLY, ("B", "2", "Input 2")),
    ],
)
def test_re_source_matches(reply: str, expected: tuple[str, str, str]) -> None:
    """Source names contain spaces, so the name group runs to end of string."""
    match = const.RE_SOURCE.search(reply)
    assert match is not None, f"RE_SOURCE did not match {reply!r}"
    assert match.groups() == expected


@pytest.mark.parametrize(
    ("reply", "expected"),
    [("Power status :On", "On"), ("Power status :Off", "Off")],
)
def test_re_amp_power_matches(reply: str, expected: str) -> None:
    """The one literal docs/protocol.md records for the amp power query."""
    match = const.RE_AMP_POWER.search(reply)
    assert match is not None, f"RE_AMP_POWER did not match {reply!r}"
    assert match.group(1) == expected


def test_fifty_byte_nul_padded_reply_decodes_with_padding_stripped() -> None:
    """The wire form is 50 bytes exactly; the payload is what precedes the NULs."""
    raw = padded(VOLUME_QUERY_REPLY)
    assert len(raw) == 50
    assert raw.endswith(b"\x00")
    assert protocol._strip(raw) == VOLUME_QUERY_REPLY


def test_all_nul_reply_decodes_to_nothing() -> None:
    """50 NULs carry no payload -- that is a group saying it has no channels."""
    assert protocol._strip(NUL_REPLY) == ""


# ---------------------------------------------------------------------------
# Queries over a fake transport
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reply",
    [VOLUME_QUERY_REPLY, VOLUME_ECHO_REPLY],
    ids=["query_form", "command_echo_form"],
)
async def test_get_volume_parses_both_reply_forms(reply: str) -> None:
    link, patcher = fake_link({FRAME_QUERY_VOLUME_D: padded(reply)})
    client = SonanceProtocol("192.0.2.10", 52000)
    with patcher:
        assert await client.get_volume(3) == -27
    assert link.frames == [FRAME_QUERY_VOLUME_D]


async def test_get_mute_parses_reply() -> None:
    link, patcher = fake_link({FRAME_QUERY_MUTE_D: padded(MUTE_QUERY_REPLY)})
    client = SonanceProtocol("192.0.2.10", 52000)
    with patcher:
        assert await client.get_mute(3) is True
    assert link.frames == [FRAME_QUERY_MUTE_D]


async def test_get_source_returns_the_name_and_not_the_digit() -> None:
    """``Src1=`` is a fixed label, so the digit must not be returned.

    Selecting source 2 still answers ``Src1=``. A client that returned the
    digit would report source 1 three times out of four; the NAME is the only
    field that identifies which input is live.
    """
    link, patcher = fake_link({FRAME_QUERY_SOURCE_D: padded(SOURCE_QUERY_REPLY)})
    client = SonanceProtocol("192.0.2.10", 52000)
    with patcher:
        assert await client.get_source(3) == "Streamer L Digital"
    assert link.frames == [FRAME_QUERY_SOURCE_D]


async def test_get_amp_power_parses_reply() -> None:
    link, patcher = fake_link({FRAME_AMP_POWER_QUERY: padded(AMP_POWER_REPLY)})
    client = SonanceProtocol("192.0.2.10", 52000)
    with patcher:
        assert await client.get_amp_power() is True
    assert link.frames == [FRAME_AMP_POWER_QUERY]


async def test_group_answering_with_fifty_nuls_is_absent() -> None:
    """None, not 0 -- a group with no channels is not a zone at minimum volume."""
    _link, patcher = fake_link({FRAME_QUERY_VOLUME_D: NUL_REPLY})
    client = SonanceProtocol("192.0.2.10", 52000)
    with patcher:
        assert await client.get_volume(3) is None
        assert await client.get_mute(3) is None


async def test_one_connection_serves_many_queries() -> None:
    """The amp allows exactly one session, so the client must reuse it.

    A second concurrent socket makes the FIRST one receive both replies, which
    surfaces as wrong values rather than as an error.
    """
    link, patcher = fake_link(
        {
            FRAME_QUERY_VOLUME_D: padded(VOLUME_QUERY_REPLY),
            FRAME_QUERY_MUTE_D: padded(MUTE_QUERY_REPLY),
            FRAME_QUERY_SOURCE_D: padded(SOURCE_QUERY_REPLY),
        }
    )
    client = SonanceProtocol("192.0.2.10", 52000)
    with patcher:
        state = await client.read_group(3)
    assert (state.volume_db, state.muted, state.source_name) == (
        -27,
        True,
        "Streamer L Digital",
    )
    assert link.open_connection.await_count == 1


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("requested", "expected_db"),
    [(99, 12), (13, 12), (-71, -70), (-1000, -70), (12, 12), (-70, -70)],
)
async def test_set_volume_clamps_rather_than_raising(
    requested: int, expected_db: int
) -> None:
    """The ceiling is a user option; a slider rounding error must not raise."""
    expected = b"\xff\x55\x02" + bytes((db_to_byte(expected_db), 0x03))
    link, patcher = fake_link({expected: padded(VOLUME_ECHO_REPLY)})
    client = SonanceProtocol("192.0.2.10", 52000)
    with patcher:
        await client.set_volume(3, requested)
    assert link.frames == [expected]


async def test_set_volume_all_is_a_global_frame() -> None:
    """LEN 01, no operand: one command sets every group."""
    expected = b"\xff\x55\x01\xb7"
    link, patcher = fake_link({expected: padded(VOLUME_ECHO_REPLY)})
    client = SonanceProtocol("192.0.2.10", 52000)
    with patcher:
        await client.set_volume_all(0)
    assert link.frames == [expected]


@pytest.mark.parametrize("source", [0, 5, -1])
async def test_set_source_rejects_out_of_range(source: int) -> None:
    """Unlike volume, a bad source number is a caller error, not a slider."""
    link, patcher = fake_link({})
    client = SonanceProtocol("192.0.2.10", 52000)
    with patcher, pytest.raises(ValueError):
        await client.set_source(3, source)
    assert link.frames == []


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


@pytest.fixture
def fast_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the silence timeout so eight groups do not take twelve seconds.

    Read at call time inside discover_groups, so patching the module global
    works; REPLY_TIMEOUT is a default argument and is NOT patchable this way.
    """
    monkeypatch.setattr(protocol, "DISCOVERY_TIMEOUT", 0.02)
    monkeypatch.setattr(protocol, "COMMAND_SPACING", 0.0)


async def test_silent_group_times_out_and_is_absent(fast_discovery: None) -> None:
    """Silence is the enumeration mechanism, not an error."""
    link, patcher = fake_link(
        {query_volume_frame(0): padded("Cmd:Volume      ,Group:A Vol=-27 db")}
    )
    client = SonanceProtocol("192.0.2.10", 52000)
    with patcher:
        found = await client.discover_groups()
    assert found == [0]
    assert len(link.frames) == const.MAX_GROUPS


async def test_discover_groups_returns_only_groups_that_answered(
    fast_discovery: None,
) -> None:
    """A mix of the three answers: a volume, 50 NULs, and nothing at all."""
    link, patcher = fake_link(
        {
            query_volume_frame(0): padded("Cmd:Volume      ,Group:A Vol=-27 db"),
            query_volume_frame(1): NUL_REPLY,  # padding only: no channels
            query_volume_frame(3): padded("Cmd:Volume      ,Group:D Vol=-40 db"),
            query_volume_frame(4): padded("Cmd:Volume      ,Group:E Vol=0 db"),
            # 2, 5, 6, 7 answer with silence
        }
    )
    client = SonanceProtocol("192.0.2.10", 52000)
    with patcher:
        found = await client.discover_groups()
    assert found == [0, 3, 4]
    assert link.frames == [query_volume_frame(g) for g in range(const.MAX_GROUPS)]


async def test_discovery_must_not_credit_a_group_with_another_groups_late_reply(
    fast_discovery: None,
) -> None:
    """A reply that arrives after its timeout is read as the NEXT group's.

    Discovery tolerates silence without resetting the connection, so a slow
    group A leaves its 50 bytes in the buffer and group B's read consumes them.
    The frame says ``Group:A``; nothing checks that against what was asked, so
    B is enumerated on the strength of A's reply -- the exact "one zone's
    volume reported as another's" failure the serialised design exists to
    prevent.

    Which group swallows it depends only on when the bytes land, so the claim
    is deliberately timing-independent: a reply that identifies itself as
    ``Group:A`` may enumerate group A and nothing else.
    """
    _link, patcher = fake_link(
        {query_volume_frame(0): (0.05, padded("Cmd:Volume      ,Group:A Vol=-27 db"))}
    )
    client = SonanceProtocol("192.0.2.10", 52000)
    with patcher:
        found = await client.discover_groups()
    assert set(found) <= {0}, (
        f"discovery returned {found}: a group was enumerated from a reply "
        "labelled Group:A. The group letter in the reply is never checked "
        "against the group that was queried."
    )


# ---------------------------------------------------------------------------
# Shutdown and cancellation
#
# These cover the two invariants that keep the amplifier's SINGLE control
# session recoverable. Both were fixed in response to a defect rather than
# designed in, and neither had a test until now -- which is the whole reason
# they are here: nothing stopped a refactor quietly undoing either one.
# ---------------------------------------------------------------------------


async def test_cancellation_mid_read_tears_the_socket_down() -> None:
    """An outside cancel must not leave a reply on the wire.

    ``asyncio.timeout`` converts a cancel into ``TimeoutError`` only when its
    OWN deadline fired. A cancel from elsewhere -- Home Assistant cancelling a
    coordinator refresh on reload, or a script stopped mid ``volume_set`` --
    passes straight through the ``TimeoutError`` and ``OSError`` handlers.

    ``readexactly`` does not consume its buffer until it holds all 50 bytes, so
    if the socket stayed open the reply would land afterwards and be handed to
    the NEXT command. That is the permanent one-ahead desync the whole design
    exists to prevent, arriving through the one door that skips the reset.
    """
    # An empty script means the amplifier never answers, so the read stays
    # parked in readexactly for us to cancel. Deliberately not a *delayed*
    # reply: that would leave a timer outstanding past the end of the test,
    # and the assertion here is about the socket, not about the reply.
    link, patcher = fake_link({})
    client = SonanceProtocol("192.0.2.10", 52000)
    with patcher:
        await client.connect()
        assert client.is_connected

        task = asyncio.create_task(client.get_volume(0))
        await asyncio.sleep(0.05)  # let it reach readexactly
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        link.writer.close.assert_called()
        assert not client.is_connected, (
            "a cancelled read left the socket open; the in-flight reply will be "
            "served to the next command"
        )


async def test_cancellation_does_not_leave_the_lock_held() -> None:
    """A cancelled command must not wedge every later command.

    The teardown runs inside the locked region. If cancellation escaped without
    releasing the lock, the integration would look alive and answer nothing.
    """
    _link, patcher = fake_link({})
    client = SonanceProtocol("192.0.2.10", 52000)
    with patcher:
        await client.connect()
        task = asyncio.create_task(client.get_volume(0))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # The lock is free, so a later command gets as far as reconnecting.
        assert not client._lock.locked()


async def test_disconnect_is_terminal() -> None:
    """After disconnect(), a command raises instead of reopening the session.

    Without this, a service call still queued on the lock at unload reopens the
    amplifier's one session behind a client nothing will ever close again.
    """
    reply = padded("Cmd:Volume      ,Group:A Vol=-27 db")
    _link, patcher = fake_link({query_volume_frame(0): reply})
    client = SonanceProtocol("192.0.2.10", 52000)
    with patcher:
        await client.connect()
        assert await client.get_volume(0) == -27
        await client.disconnect()

        with pytest.raises(SonanceConnectionError):
            await client.get_volume(0)


async def test_disconnect_during_connect_does_not_orphan_the_socket() -> None:
    """disconnect() landing mid-connect must not leave a live socket behind.

    disconnect() is lock-free by necessity -- _request calls _abort from inside
    the locked region and asyncio.Lock is not reentrant -- so it can run while
    another task is suspended in open_connection. It then reads self._writer as
    None, closes nothing, and returns.

    Before the fix the socket that opened a moment later was published onto a
    client already closed for good, and nothing ever closed it. On an amplifier
    that accepts exactly one control session, that orphan locks out every later
    setup until Home Assistant restarts.
    """
    link = FakeLink({})
    opened: list[MagicMock] = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_open(*_args, **_kwargs):
        started.set()
        await release.wait()  # hold the connect open across the disconnect
        opened.append(link.writer)
        return link.reader, link.writer

    client = SonanceProtocol("192.0.2.10", 52000)
    with patch("asyncio.open_connection", slow_open):
        connecting = asyncio.create_task(client.connect())
        await started.wait()

        await client.disconnect()  # lands while open_connection is suspended
        release.set()

        with pytest.raises(SonanceConnectionError):
            await connecting

    assert not client.is_connected
    assert opened, "the test did not exercise the race it describes"
    link.writer.close.assert_called(), "the orphaned socket was never closed"


# ---------------------------------------------------------------------------
# Padded replies
#
# Not every reply is one frame. A source CHANGE answers with 256 bytes: the
# payload in the first 50 and 206 NUL bytes behind it, on opcodes 0x0A/0x0B/0x0C
# but not 0x09. Measured on the device, in a single TCP segment.
# ---------------------------------------------------------------------------


def source_frame(source: int, group: int) -> bytes:
    return bytes((0xFF, 0x55, 0x02, 0x08 + source, group))


async def test_set_source_drains_the_padding_behind_its_reply() -> None:
    """The 206 NUL bytes must not be left for the next four commands.

    Without the drain they are read as replies: four commands in a row see
    pure padding, decode to nothing, and look exactly like "no reply" -- which
    on a query means "this group has no channels".
    """
    padded_reply = padded("Cmd:Source2     , Group:D") + b"\x00" * 206
    _link, patcher = fake_link(
        {
            source_frame(2, 3): padded_reply,
            query_volume_frame(3): padded("Cmd:Volume      ,Group:D Vol=-27 db"),
        }
    )
    client = SonanceProtocol("192.0.2.10", 52000)
    with patcher:
        await client.connect()
        await client.set_source(3, 2)

        # The very next command must get its OWN reply, not leftover padding.
        assert await client.get_volume(3) == -27


async def test_unpadded_reply_costs_nothing_extra() -> None:
    """Selecting source 1 replies with a bare 50 bytes; the drain must cope."""
    _link, patcher = fake_link(
        {
            source_frame(1, 3): padded("Cmd:Source1     , Group:D"),
            query_volume_frame(3): padded("Cmd:Volume      ,Group:D Vol=-27 db"),
        }
    )
    client = SonanceProtocol("192.0.2.10", 52000)
    with patcher:
        await client.connect()
        await client.set_source(3, 1)
        assert await client.get_volume(3) == -27


async def test_non_padding_leftovers_reset_the_connection() -> None:
    """Trailing bytes that are not NUL mean a real frame was left behind.

    That is a genuine desync rather than padding, and continuing would read
    someone else's reply as the next answer.
    """
    trailing = padded("Cmd:Source2     , Group:D") + padded(
        "Cmd:Volume      ,Group:A Vol=-40 db"
    )
    _link, patcher = fake_link({source_frame(2, 3): trailing})
    client = SonanceProtocol("192.0.2.10", 52000)
    with patcher:
        await client.connect()
        with pytest.raises(SonanceConnectionError, match="out of step"):
            await client.set_source(3, 2)
        assert not client.is_connected


@pytest.mark.parametrize("source", [0, 5, -1])
async def test_set_source_rejects_out_of_range_before_the_socket(
    source: int,
) -> None:
    link, patcher = fake_link({})
    client = SonanceProtocol("192.0.2.10", 52000)
    with patcher:
        await client.connect()
        with pytest.raises(ValueError):
            await client.set_source(0, source)
        assert link.frames == []

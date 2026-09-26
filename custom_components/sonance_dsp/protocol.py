"""Async TCP client for the Sonance DSP binary protocol.

See ``docs/protocol.md`` for the protocol itself and the evidence behind it,
and ``docs/design.md`` for why this client is shaped the way it is.

Three measured constraints drive everything here:

* **One connection.** A second concurrent socket causes the *first* socket to
  receive both replies while the second receives nothing. The failure mode is
  wrong values, not an error, so this is a correctness requirement.
* **Fixed-width reads.** Replies are exactly 50 bytes, NUL-padded, with no
  terminator. ``readline()`` waits forever.
* **Positional correlation, checked against the group letter.** Replies carry
  no sequence number or request id, so the Nth reply belongs to the Nth
  command. But scoped replies *do* echo their ``Group:`` letter, and that one
  field is enough to prove alignment after the fact. Every scoped getter checks
  it. An earlier version of this module parsed the letter and threw it away,
  which is how a single late reply could shift the whole stream silently.

Commands are serialised by a lock rather than pipelined. The device does
pipeline correctly, but a command that gets no reply leaves a queue-based
design permanently one ahead, resolving every later reply against the wrong
request. Since a group with no channels answers with silence, no-reply is a
normal path here and not an error path. Serialising costs a round trip per
command and makes a timeout local instead of corrupting.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from .const import (
    FORBIDDEN_OPCODES,
    FRAME_PREFIX,
    GROUP_LETTERS,
    LEN_GLOBAL,
    LEN_SCOPED,
    MAX_GROUPS,
    MAX_VOLUME_DB,
    MIN_VOLUME_DB,
    OP_AMP_POWER_OFF,
    OP_AMP_POWER_ON,
    OP_AMP_POWER_QUERY,
    OP_GROUP_OFF,
    OP_GROUP_ON,
    OP_MUTE_OFF,
    OP_MUTE_ON,
    OP_QUERY_MUTE,
    OP_QUERY_SOURCE,
    OP_QUERY_VOLUME,
    OP_SOURCE_BASE,
    OP_VOLUME_DOWN,
    OP_VOLUME_UP,
    RE_AMP_POWER,
    RE_MUTE,
    RE_SOURCE,
    RE_VOLUME,
    REPLY_LENGTH,
    SOURCE_COUNT,
    db_to_byte,
)

_LOGGER = logging.getLogger(__name__)

CONNECT_TIMEOUT = 10.0
REPLY_TIMEOUT = 3.0
DISCOVERY_TIMEOUT = 1.5
# docs/protocol.md records 100-200 ms after a query before the next command.
# Below that a slow reply can land after its own deadline, which is exactly the
# case the group-letter check exists to catch.
COMMAND_SPACING = 0.12
# How long to wait for leftover padding before sending a new command. Only
# ever spent when the previous reply was longer than one frame, because the
# read below returns immediately once the buffer is empty.
STALE_DRAIN_TIMEOUT = 0.05


class SonanceError(Exception):
    """Base error for the Sonance protocol."""


class SonanceConnectionError(SonanceError):
    """The connection failed, was lost, or the reply stream lost alignment."""


class ForbiddenOpcodeError(SonanceError):
    """A destructive opcode was requested.

    Raised rather than returned: these opcodes echo success for changes they
    did not apply, so a caller that reached this point cannot be allowed to
    proceed and verify afterwards.
    """


@dataclass(frozen=True, slots=True)
class GroupState:
    """State of one output group, as far as TCP can report it.

    ``None`` means "not read this cycle", never "off" -- a group that stopped
    answering must not render as a zone at zero volume.
    """

    group: int
    volume_db: int | None = None
    muted: bool | None = None
    # The NAME only. The protocol's source number cannot be trusted -- see
    # get_source. Callers that need the number resolve it from the name.
    source_name: str | None = None

    @property
    def answered(self) -> bool:
        """True when this cycle got a real volume reading for the group.

        Deliberately a named property rather than ``__bool__``: the object is
        always truthy, and call sites that mean "did we read a volume" must say
        so rather than relying on the container existing.
        """
        return self.volume_db is not None


def _strip(raw: bytes) -> str:
    """Decode a 50-byte reply, dropping NUL padding and trailing spaces."""
    return raw.rstrip(b"\x00").decode("ascii", errors="replace").strip()


class SonanceProtocol:
    """A single, exclusive control connection to a Sonance DSP amplifier."""

    def __init__(self, host: str, port: int) -> None:
        self._host = host
        self._port = port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        # Serialises write-then-read. See the module docstring.
        self._lock = asyncio.Lock()
        self._connected = False
        # Terminal once disconnect() has been called. Without this, a service
        # call still queued on the lock at unload reopens the amplifier's one
        # session behind an orphaned client that nothing will ever close.
        self._closed = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    def _set_connected(self, value: bool) -> None:
        self._connected = value

    # --- connection --------------------------------------------------------

    async def connect(self) -> None:
        """Open the one control connection, clearing any previous close."""
        async with self._lock:
            self._closed = False
            await self._connect_locked()

    async def _connect_locked(self) -> None:
        """Open the socket. Caller must hold the lock."""
        if self._connected:
            return
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT):
                reader, writer = await asyncio.open_connection(self._host, self._port)
        except (TimeoutError, OSError) as err:
            self._reader = self._writer = None
            raise SonanceConnectionError(
                f"Could not connect to {self._host}:{self._port}: {err}"
            ) from err

        # disconnect() is lock-free -- it has to be, because _request calls
        # _abort from inside the locked region and asyncio.Lock is not
        # reentrant. That means disconnect() can land while this coroutine is
        # suspended in open_connection above: it reads self._writer as None,
        # closes nothing, and returns. Without this re-check the socket that
        # just opened would be published onto a client already closed for
        # good, and nothing would ever close it.
        #
        # On this amplifier that is the worst leak available. It accepts one
        # control session, so an orphaned socket locks out every later setup
        # until Home Assistant restarts.
        if self._closed:
            writer.close()
            raise SonanceConnectionError(
                "Connection was closed while connecting; discarding the new socket"
            )

        self._reader, self._writer = reader, writer
        self._set_connected(True)
        _LOGGER.debug("Connected to %s:%s", self._host, self._port)

    def _abort(self) -> None:
        """Tear the socket down synchronously.

        Deliberately not a coroutine. This runs from cancellation paths, where
        awaiting anything is unreliable -- a shielded await can still be
        re-cancelled, and at shutdown it usually is. ``writer.close()`` needs no
        await and the transport finishes closing on its own; what matters is
        that the amplifier's single session is released and that no later
        command reuses a stream whose alignment we can no longer vouch for.
        """
        writer, self._writer, self._reader = self._writer, None, None
        self._set_connected(False)
        if writer is not None:
            writer.close()

    async def disconnect(self) -> None:
        """Close for good. Further commands raise rather than reconnecting.

        Lock-free on purpose: ``_request`` calls ``_abort`` from inside the
        locked region, and ``asyncio.Lock`` is not reentrant.
        """
        self._closed = True
        writer = self._writer
        self._abort()
        if writer is None:
            return
        try:
            async with asyncio.timeout(5):
                await writer.wait_closed()
        except (TimeoutError, OSError) as err:
            _LOGGER.debug("Error closing connection: %s", err)

    # --- framing -----------------------------------------------------------

    @staticmethod
    def build_frame(opcode: int, operand: int | None = None) -> bytes:
        """Build one protocol frame.

        Refuses FORBIDDEN_OPCODES here rather than at the call sites, because
        the whole danger of those opcodes is that a mistake is invisible at the
        point of the mistake.
        """
        if opcode in FORBIDDEN_OPCODES:
            raise ForbiddenOpcodeError(
                f"Opcode 0x{opcode:02X} reassigns channels between groups. It is "
                "destructive, it echoes success for changes it does not apply, and "
                "it has no safe inverse. Change group topology in the amplifier's "
                "web UI instead."
            )
        if operand is None:
            return FRAME_PREFIX + bytes((LEN_GLOBAL, opcode))
        return FRAME_PREFIX + bytes((LEN_SCOPED, opcode, operand))

    # --- transport ---------------------------------------------------------

    async def _request(
        self,
        opcode: int,
        operand: int | None = None,
        *,
        timeout: float = REPLY_TIMEOUT,
        require_reply: bool = True,
        tolerate_silence: bool = False,
        drain_after: bool = False,
    ) -> str | None:
        """Send one frame and return its reply.

        ``require_reply`` defaults to True so a newly added setter fails loudly
        rather than silently: silence is a legitimate answer to a *query* on an
        unpopulated group, and never a legitimate answer to a *write*. A write
        that was not echoed was not acknowledged.

        ``tolerate_silence`` is for group discovery, where no reply is the
        expected answer for an empty group and must not tear the socket down.

        ``drain_after`` is for the opcodes whose reply is longer than one frame
        -- see ``_discard_padding``. It is opt-in because the drain costs a
        timeout every time it runs, so paying it on every command would add
        roughly half a second to each poll cycle.
        """
        frame = self.build_frame(opcode, operand)
        async with self._lock:
            if self._closed:
                raise SonanceConnectionError("Connection has been closed")
            if not self._connected:
                await self._connect_locked()
            assert self._reader is not None and self._writer is not None
            try:
                self._writer.write(frame)
                await self._writer.drain()
                async with asyncio.timeout(timeout):
                    raw = await self._reader.readexactly(REPLY_LENGTH)
                if drain_after:
                    await self._discard_padding()
            except TimeoutError:
                # NB TimeoutError is an OSError subclass, so this clause must
                # stay above the OSError one.
                if tolerate_silence:
                    return None
                self._abort()
                if require_reply:
                    raise SonanceConnectionError(
                        f"No reply to opcode 0x{opcode:02X}; command not acknowledged"
                    ) from None
                return None
            except (OSError, asyncio.IncompleteReadError) as err:
                self._abort()
                raise SonanceConnectionError(f"Connection lost: {err}") from err
            except BaseException:
                # Cancellation from outside -- HA cancelling a coordinator
                # refresh on reload, or a script stopped mid service call.
                # asyncio.timeout only converts a cancel into TimeoutError when
                # its OWN deadline fired, so this path is reached with a reply
                # still on the wire. Leaving the socket open would hand that
                # reply to the next command.
                self._abort()
                raise

        text = _strip(raw)
        # A group with no channels can answer with 50 NUL bytes rather than
        # with silence. Both mean absent.
        return text or None

    async def _discard_padding(self) -> int:
        """Drop the padding behind a reply that is longer than one frame.

        Not every reply is 50 bytes. A source CHANGE answers with 256 -- the
        payload in the first frame and 206 NUL bytes behind it, on opcodes
        0x0A/0x0B/0x0C but not 0x09. Reading a fixed 50 leaves that padding
        queued, so the next four commands read pure padding and look like "no
        reply" before the fifth resynchronises.

        Safe to run immediately after the read because the whole 256 bytes
        arrive in a single TCP segment -- measured, one recv -- so the padding
        is already buffered by the time readexactly returns.

        Opt-in rather than automatic: ``StreamReader.read`` WAITS on an empty
        buffer rather than returning, so an unconditional drain would spend its
        timeout on every command. At 13 commands a cycle that is most of a
        second per poll, for padding that only two or three opcodes produce.
        """
        assert self._reader is not None
        dropped = 0
        while True:
            try:
                async with asyncio.timeout(STALE_DRAIN_TIMEOUT):
                    chunk = await self._reader.read(REPLY_LENGTH * 8)
            except TimeoutError:
                break
            if not chunk:
                break
            dropped += len(chunk)
            if chunk.strip(b"\x00"):
                # Padding is all NUL. Anything else means a real frame was left
                # behind, so the stream is out of step rather than merely
                # padded, and continuing would read it as the next reply.
                self._abort()
                raise SonanceConnectionError(
                    f"Discarded {dropped} unread byte(s) containing non-padding "
                    "data; the reply stream was out of step, connection reset"
                )
        if dropped:
            _LOGGER.debug("Discarded %d byte(s) of trailing frame padding", dropped)
        return dropped

    def _desync(self, expected: int, got: str) -> None:
        """Abort the connection after a reply arrived for the wrong group."""
        self._abort()
        raise SonanceConnectionError(
            f"Reply for group {got} arrived for a query on "
            f"{GROUP_LETTERS[expected]}; reply stream misaligned, connection reset"
        )

    def _unparsed(self, what: str, group: int | None, reply: str) -> None:
        """Log a reply that arrived but did not match its expected format.

        Distinguishing this from silence is what turns an unverified reply
        format into a one-line bug report rather than a permanently dead
        control. Mute and source formats have no recorded device literal behind
        them.
        """
        where = f" for group {GROUP_LETTERS[group]}" if group is not None else ""
        _LOGGER.warning(
            "Unrecognised %s reply%s: %r. Please report this with your "
            "amplifier model and firmware version",
            what,
            where,
            reply,
        )

    # --- volume ------------------------------------------------------------

    async def get_volume(self, group: int) -> int | None:
        """Read a group's volume in dB, or None if the group has no channels."""
        reply = await self._request(OP_QUERY_VOLUME, group, require_reply=False)
        if reply is None:
            return None
        match = RE_VOLUME.search(reply)
        if not match:
            self._unparsed("volume", group, reply)
            return None
        if match.group(1) != GROUP_LETTERS[group]:
            self._desync(group, match.group(1))
        return int(match.group(2))

    async def set_volume(self, group: int, db: int) -> None:
        """Set a group's volume.

        Absolute set is verified working on firmware V2.2.8130, despite the
        vendor spreadsheet documenting it only for V2.51.
        """
        await self._request(db_to_byte(_clamp_db(db)), group)

    async def set_volume_all(self, db: int) -> None:
        """Set every group's volume with one amplifier-wide command."""
        await self._request(db_to_byte(_clamp_db(db)), None)

    async def volume_up(self, group: int) -> None:
        await self._request(OP_VOLUME_UP, group)

    async def volume_down(self, group: int) -> None:
        await self._request(OP_VOLUME_DOWN, group)

    # --- mute --------------------------------------------------------------

    async def get_mute(self, group: int) -> bool | None:
        reply = await self._request(OP_QUERY_MUTE, group, require_reply=False)
        if reply is None:
            return None
        match = RE_MUTE.search(reply)
        if not match:
            self._unparsed("mute", group, reply)
            return None
        if match.group(1).upper() != GROUP_LETTERS[group]:
            self._desync(group, match.group(1))
        return match.group(2).lower() == "on"

    async def set_mute(self, group: int, mute: bool) -> None:
        await self._request(OP_MUTE_ON if mute else OP_MUTE_OFF, group)

    # --- source ------------------------------------------------------------

    async def get_source(self, group: int) -> str | None:
        """Return the NAME of the group's selected source.

        The name, and deliberately not the number. ``Src1=`` is a fixed label:
        selecting source 2 still answers ``Cmd:Source1 ,Group:D Src1=Input 2L``,
        so the digit is 1 whatever is selected. Returning it would be returning
        a value that is wrong three times out of four.

        Resolve the number from the name via the HTTP channel layout --
        ``Topology.source_number_for_input_name``.
        """
        reply = await self._request(OP_QUERY_SOURCE, group, require_reply=False)
        if reply is None:
            return None
        match = RE_SOURCE.search(reply)
        if not match:
            self._unparsed("source", group, reply)
            return None
        if match.group(1).upper() != GROUP_LETTERS[group]:
            self._desync(group, match.group(1))
        return match.group(3).strip()

    async def set_source(self, group: int, source: int) -> None:
        """Select a source for a group.

        ``drain_after`` because this is the command measured to reply with 256
        bytes rather than 50; without it the next four commands read its
        padding instead of their own replies.
        """
        if not 1 <= source <= SOURCE_COUNT:
            raise ValueError(f"Source must be 1-{SOURCE_COUNT}, got {source}")
        await self._request(OP_SOURCE_BASE + source, group, drain_after=True)

    # --- power -------------------------------------------------------------

    async def get_amp_power(self) -> bool | None:
        """Read amplifier standby state.

        There is no per-GROUP power query in this protocol at all; read that
        from the HTTP status page instead.
        """
        reply = await self._request(OP_AMP_POWER_QUERY, None, require_reply=False)
        if reply is None:
            return None
        match = RE_AMP_POWER.search(reply)
        if not match:
            self._unparsed("amplifier power", None, reply)
            return None
        return match.group(1).lower() == "on"

    async def set_amp_power(self, on: bool) -> None:
        await self._request(OP_AMP_POWER_ON if on else OP_AMP_POWER_OFF, None)

    async def set_group_power(self, group: int, on: bool) -> None:
        await self._request(OP_GROUP_ON if on else OP_GROUP_OFF, group)

    # --- discovery ---------------------------------------------------------

    async def discover_groups(self) -> list[int]:
        """Return the indices of groups that have channels assigned.

        A group with no channels answers with silence or with NUL padding. A
        group that is merely slow answers late -- and a late reply is
        indistinguishable from the next group's reply unless the echoed letter
        is checked, which is what makes the letter comparison below load-bearing
        rather than defensive. Without it, one slow group shifts the whole
        enumeration: zone A disappears and a phantom zone C is invented.
        """
        found: list[int] = []
        for group in range(MAX_GROUPS):
            reply = await self._request(
                OP_QUERY_VOLUME,
                group,
                timeout=DISCOVERY_TIMEOUT,
                require_reply=False,
                tolerate_silence=True,
            )
            if reply:
                match = RE_VOLUME.search(reply)
                if match and match.group(1) != GROUP_LETTERS[group]:
                    # A late reply from an earlier group. Discard it and carry
                    # on rather than raising: the stream is one behind, so the
                    # remaining groups may under-report, and the coordinator's
                    # cross-check against the authoritative HTTP channel map
                    # repairs that. Raising here would turn a momentarily slow
                    # amplifier into a failed setup for a condition that is
                    # already recoverable.
                    _LOGGER.warning(
                        "Reply for group %s arrived while querying group %s; "
                        "discarding it. Enumeration may be incomplete and will "
                        "be reconciled against the amplifier's channel map",
                        match.group(1),
                        GROUP_LETTERS[group],
                    )
                elif match:
                    found.append(group)
            await asyncio.sleep(COMMAND_SPACING)
        return found

    async def read_group(self, group: int) -> GroupState:
        """Read the full TCP-visible state of one group."""
        return GroupState(
            group=group,
            volume_db=await self.get_volume(group),
            muted=await self.get_mute(group),
            source_name=await self.get_source(group),
        )


def _clamp_db(db: int) -> int:
    """Clamp to the device's range.

    Clamping rather than raising: the ceiling is a user-set option, and a
    rounding error at the top of a slider should not raise into a service call.
    """
    return max(MIN_VOLUME_DB, min(MAX_VOLUME_DB, int(db)))

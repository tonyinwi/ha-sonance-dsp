"""Constants for the Sonance DSP integration.

Every value here was verified against a live DSP 8-130 MKII (firmware V2.2.8130)
on 2026-09-20, not transcribed from the vendor documentation. Where the two
disagree, the device won and the disagreement is noted.

See ``docs/protocol.md`` for the full reference and the evidence behind it.
"""

from __future__ import annotations

import re
from typing import Final

DOMAIN: Final = "sonance_dsp"

# --- Transport -------------------------------------------------------------
# TCP carries control. HTTP carries identity, names and per-group power, which
# the TCP protocol genuinely cannot report.
DEFAULT_TCP_PORT: Final = 52000
DEFAULT_HTTP_PORT: Final = 80

HTTP_HANDLER_PATH: Final = "/Web/Handler.php"
HTTP_PAGE_STATUS: Final = "status"
# NOT "basicsettings". That page exists and answers, but it is an older,
# cut-down view: it omits turn-on volume, maximum volume, gain offset,
# level trim, stereo/mono, bridge mode and the second source slot. The
# In/Out Settings tab reads this one instead, and Landing.htm does not link
# to it -- it is reachable only from inside GeneralSettings.htm, which is
# why the lesser endpoint is the easy one to find.
HTTP_PAGE_IN_OUT: Final = "in-out-settings"
HTTP_PAGE_GENERAL: Final = "general-settings"

# The amp accepts exactly ONE TCP session. A second concurrent connection
# receives nothing AND its replies are delivered to the first socket, silently
# corrupting that socket's reply stream. Never open a second connection.
MAX_CONNECTIONS: Final = 1

# Replies are a fixed 50 bytes, NUL-padded, with NO terminator. There is
# nothing to readline() on -- a line-oriented reader hangs forever.
REPLY_LENGTH: Final = 50

# --- Frame format ----------------------------------------------------------
# FF 55 <LEN> <OPCODE> [<OPERAND>]   -- no terminator, no checksum.
FRAME_PREFIX: Final = b"\xff\x55"
LEN_GLOBAL: Final = 0x01  # opcode only, applies to the whole amplifier
LEN_SCOPED: Final = 0x02  # opcode + one operand (a group or a channel)

# --- Group addressing ------------------------------------------------------
# Groups are 0-based: A=0x00 ... H=0x07. Up to 8; only populated ones reply.
GROUP_LETTERS: Final = "ABCDEFGH"
MAX_GROUPS: Final = 8

# Channel operands, 1L..4R.
CHANNEL_OPERANDS: Final = {
    "1L": 0x08, "1R": 0x09,
    "2L": 0x0A, "2R": 0x0B,
    "3L": 0x0C, "3R": 0x0D,
    "4L": 0x0E, "4R": 0x0F,
}

# --- Volume ----------------------------------------------------------------
# byte = dB + 183.  -70 -> 0x71, -27 -> 0x9C, 0 -> 0xB7, +12 -> 0xC3
# Absolute set is VERIFIED WORKING on firmware V2.2.8130 (tested at -40, -55,
# -70 and back to -27, all four exact on read-back), despite the vendor
# spreadsheet documenting it only for V2.51.
VOLUME_OFFSET: Final = 183
MIN_VOLUME_DB: Final = -70
MAX_VOLUME_DB: Final = 12

# Default ceiling for the HA 0-100% mapping. The device can reach +12 dB, but
# that is the factory turn-on default the vendor's own Savant notes flag as a
# hazard, so raising it is a deliberate change in the options flow (one ceiling
# for every zone, lowered per zone to the amplifier's own maximum).
DEFAULT_MAX_DB: Final = 0


def db_to_byte(db: int) -> int:
    """Convert a dB level to its protocol byte."""
    return db + VOLUME_OFFSET


def byte_to_db(value: int) -> int:
    """Convert a protocol byte back to a dB level."""
    return value - VOLUME_OFFSET


# --- Opcodes ---------------------------------------------------------------
# Amplifier-wide (LEN_GLOBAL, no operand)
OP_AMP_POWER_ON: Final = 0x01
OP_AMP_POWER_OFF: Final = 0x02
OP_AMP_POWER_TOGGLE: Final = 0x03
OP_AMP_POWER_QUERY: Final = 0x70

# Volume, relative. Single step, not a press-and-hold ramp.
OP_VOLUME_UP: Final = 0x04
OP_VOLUME_DOWN: Final = 0x05
OP_VOLUME_UP_3DB: Final = 0x0E
OP_VOLUME_DOWN_3DB: Final = 0x0F
OP_RECALL_TURN_ON_VOLUME: Final = 0x0D

# Mute
OP_MUTE_TOGGLE: Final = 0x06
OP_MUTE_ON: Final = 0x07
OP_MUTE_OFF: Final = 0x08

# Source select: opcode = 0x08 + source number (1-4)
OP_SOURCE_BASE: Final = 0x08
SOURCE_COUNT: Final = 4

# Queries
OP_QUERY_VOLUME: Final = 0x10
OP_QUERY_SOURCE: Final = 0x11
OP_QUERY_MUTE: Final = 0x12

# Per-channel queries
OP_QUERY_DSP_PRESET: Final = 0x16
OP_QUERY_SHORT_PROTECT: Final = 0x17
OP_QUERY_OVERTEMP: Final = 0x18

# Group power. NOTE: there is no group-power QUERY in this protocol. Read it
# from the HTTP status page instead -- that endpoint is the only source.
OP_GROUP_ON: Final = 0x65
OP_GROUP_OFF: Final = 0x66
OP_GROUP_TOGGLE: Final = 0x67

# --- Forbidden -------------------------------------------------------------
# 0x21-0x28 reassign channels between groups (0x21 -> A, 0x22 -> B, ...).
# They are DESTRUCTIVE, there is no safe inverse without a prior backup, and
# THE ECHO LIES: sending 0x22 returned "Channel <name> group is B" for a change
# that never applied -- the authoritative HTTP output-groups still read "a".
# This integration must never send them, and no service may surface them.
# Group topology belongs in the amp's web UI.
FORBIDDEN_OPCODES: Final = frozenset(range(0x21, 0x29))

# --- Reply parsing ---------------------------------------------------------
# Two formats, and they are NOT the same. A parser that assumes one silently
# fails on the other:
#
#   query reply : "Cmd:Volume      ,Group:D Vol=-27 db"   6 spaces, space before db
#   command echo: "Cmd:VolumeUP   ,Group:D Vol=-27db"     3 spaces, NO space before db
#
# Hence \s* everywhere rather than literal spacing. Note also that an absolute
# volume set echoes as "VolumeUP" whatever direction it moved -- the Cmd: label
# cannot be used to infer what was sent.
#
# The Group: letter is a CORRELATOR, not decoration. It is the only field that
# can prove a reply belongs to the query that asked for it, so every scoped
# getter must check it rather than parsing the value and discarding the letter.
#
# Mute and Source stay IGNORECASE as cheap insurance. Their query replies are
# recorded ("Cmd:MuteState   ,Group:D Mute=on", "Cmd:Source1     ,Group:D
# Src1=<name>") -- see docs/protocol.md.
RE_VOLUME: Final = re.compile(r",\s*Group:([A-H])\s+Vol=(-?\d{1,2})\s*db")
RE_MUTE: Final = re.compile(r",\s*Group:([A-H])\s+Mute=(on|off)", re.IGNORECASE)
RE_SOURCE: Final = re.compile(r",\s*Group:([A-H])\s+Src(\d)=(.+?)\s*$", re.IGNORECASE)
RE_AMP_POWER: Final = re.compile(r"Power\s+status\s*:\s*(\w+)")

# Echoes of the writes whose acknowledgement matters, as recorded from the
# device on 2026-09-26: "Cmd:GroupON      ,Group:A", "Cmd:MuteOn      , Group:A",
# "Cmd:PowerOn". A write is acknowledged by ITS echo, not by any 50 bytes: a
# frame of NUL padding, or another group's late reply, is not an answer --
# and with the status page down, the echo is the only confirmation there is.
RE_GROUP_POWER_ECHO: Final = re.compile(
    r"Cmd:\s*Group(ON|OFF)\s*,\s*Group:([A-H])", re.IGNORECASE
)
RE_MUTE_ECHO: Final = re.compile(
    r"Cmd:\s*Mute(ON|OFF)\s*,\s*Group:([A-H])", re.IGNORECASE
)
RE_AMP_POWER_ECHO: Final = re.compile(r"Cmd:\s*Power(ON|OFF)\b", re.IGNORECASE)

# --- Power ------------------------------------------------------------------
# Measured on the device in Power Button mode (2026-09-26) unless marked.
#
# Waking from standby took 10.6 s (the manual says 9-12). Mute writes sent
# during that window were lost even though they echoed success; zone-on writes
# sent in the same window were applied. The cause was not established, so the
# policy is the conservative one: nothing but a status query until the
# amplifier reports On.
WAKE_TIMEOUT: Final = 25.0
WAKE_POLL_INTERVAL: Final = 0.5
# Amplifier-power queries before its state is declared unknown. A single late
# reply is routine; three in a row is not, and a power change made on a guess
# about whether the amp is awake is how zones nobody asked for start playing.
AMP_POWER_READ_ATTEMPTS: Final = 3
AMP_POWER_READ_INTERVAL: Final = 0.5
# After switching a muted zone on, its mute is read back at these intervals
# (cumulative 0.3, 0.6, 1.0, 1.5 s) and re-sent whenever it reads off.
# The amp holds a zone's mute off from ~0.2 s to ~1.05 s after a zone-on and
# re-applies it then (measured 2026-09-27); these reads span that window, so a
# mute that did not come back is caught and re-sent.
MUTE_VERIFY_DELAYS: Final = (0.3, 0.3, 0.4, 0.5)
# Straight after a wake the window runs to 5 s. Mute writes sent during a boot
# were lost while zone-on writes were kept, which fits the clear landing when a
# zone actually powers up -- and just after "On", that may be later.
MUTE_VERIFY_DELAYS_AFTER_WAKE: Final = (*MUTE_VERIFY_DELAYS, 1.5, 2.0)
# After a zone-on the amp applies the zone's turn-on volume at ~0.2 s -- over any
# volume sent before then -- and holds the zone's mute off until ~1.05 s, and that
# window is AUDIBLE (measured 2026-09-27, by ear). Set every zone's turn-on volume
# to -70 dB on the amp and the window is silent; the level the zone had before it
# was switched off is then restored once the window has passed, and checked.
VOLUME_RESTORE_AFTER: Final = 1.5
VOLUME_CONFIRM_DELAYS: Final = (0.3, 0.3, 0.4)
# How long unload waits for a power change in progress before disconnecting.
# Cutting one off mid-wake leaves the zones a wake revived playing.
CLOSE_WAIT: Final = WAKE_TIMEOUT + 15
# After a poll fails outright, the next one comes this soon rather than a
# whole interval later: an unavailable entity ignores turn_off.
POLL_RETRY_AFTER: Final = 5.0
# A wake that timed out is not retried for this long. Without it a scene
# switching four zones on against an amplifier that will not wake spends
# four full timeouts -- over a minute and a half -- with every zone command
# queued behind it. Any poll that finds the amplifier On clears it early.
WAKE_RETRY_HOLDOFF: Final = 60.0
# The HTTP status page reflected a group power change within 0.01-0.06 s, so
# a few seconds is generous for confirming it.
POWER_CONFIRM_TIMEOUT: Final = 3.0
POWER_CONFIRM_INTERVAL: Final = 0.2
# How long zone power last read from the status page is still shown once the
# page stops answering, or three poll intervals if that is longer. Without it
# one failed HTTP read turns every zone "unknown", which HomeKit shows as off
# and media_player.toggle answers by switching the zone OFF. Bounded, because a
# zone switched from the amp's own web UI during a long outage would otherwise
# show a stale state forever.
GROUP_POWER_STALE_AFTER: Final = 300.0
# Consecutive unanswered amplifier-power queries bridged with the last answer
# -- but only an "on" answer. A single late reply is routine and should not
# flicker every zone to unknown. A cached "standby" is NOT bridged: if the amp
# was woken from outside Home Assistant in between, bridging it would show
# zones OFF while they play. Unknown is the honest answer there.
AMP_POWER_MISSES_BRIDGED: Final = 2

# --- Config entry / options ------------------------------------------------
CONF_MAX_DB: Final = "max_db"
# Maps a source number ("1".."4") to an upstream media_player entity_id. The
# amplifier only knows it is amplifying a line input; the upstream player is
# the only thing that knows what is actually playing.
CONF_INPUT_LINKS: Final = "input_links"


def input_link_key(source: int) -> str:
    """Options-flow field name for one source's link."""
    return f"input_link_{source}"

CONF_SCAN_INTERVAL: Final = "scan_interval"
DEFAULT_SCAN_INTERVAL: Final = 10
MIN_SCAN_INTERVAL: Final = 5
MAX_SCAN_INTERVAL: Final = 300

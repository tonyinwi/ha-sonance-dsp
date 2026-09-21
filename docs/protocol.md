# Sonance DSP protocol reference

Verified against a live **DSP 8-130 MKII, firmware V2.2.8130** on 2026-09-20.

This document records what the *device* does. Where it contradicts the vendor
spreadsheet (`DSP_IP_Codes_SONANCE.xlsx`) or the Savant profile, the device wins and the
contradiction is called out — those cases are the ones that break naive implementations.

## Transports

The amplifier answers on two ports, and each knows something the other does not.

| | TCP 52000 | HTTP 80 |
|---|---|---|
| Purpose | control — set and query | identity, names, group power |
| Format | binary frames | JSON |
| Serial / model / firmware | ✗ | ✓ |
| Zone + source **names** | partial | ✓ |
| **Per-group power** | ✗ *(no such opcode)* | ✓ |
| Channel→group map | only "empty or not" | ✓ authoritative |
| Volume / mute / source set | ✓ | — |

Use **TCP for writes and fast state, HTTP for identity, discovery and group power.**

### HTTP endpoints

Undocumented in any vendor artifact — found by reading the web UI's own JavaScript.

```
GET /Web/Handler.php?page=status&action=read
GET /Web/Handler.php?page=basicsettings&action=read
GET /Web/Handler.php?page=general-settings&action=read
```

`status` returns per-group power and mute:

```json
{"status-titles":["GROUP A", ...],
 "power-status":["on","on","on","on","off","off","off","off"],
 "mute-volumes":["off","off","off","off","off","off","off","off"]}
```

`basicsettings` returns the zone topology:

```json
{"output-names":["Zone 1 L","Zone 1 R","Zone 2 L", ...],
 "input-names":["Streamer L Digital","Streamer R Digital","Input 2L", ...],
 "output-groups":["a","a","b","b","c","c","d","d"],
 "dsp-presets":[44,44,47,47,48,48,0,0],
 "output-volumes":["-27","-27", ...]}
```

`output-names` and `input-names` are installer-assigned; the values above are illustrative.
`output-groups` is the channel-to-group map, one letter per channel, 1L through 4R.

`general-settings` returns `serial-number`, `amplifier-name`, `amplifier-model`,
`firmware-version`, and network config. **Use `serial-number` as the config entry's
`unique_id`** — the amp is typically on DHCP, so the IP is not stable identity.

⚠️ There is also an `action=write` form. This integration does not use it — everything
writable is writable over TCP, and TCP is where the reply can be correlated.

Be precise about why, because the reason is narrower than it first looked. A
`name=output-group` write was observed **returning unchanged JSON for a change it did not
apply**, with the amplifier in standby. A `name=output-volume` write, by contrast, applies
reliably — verified during the push experiment. So the endpoint is not broadly unreliable;
one field is, and the safe rule is to read over HTTP and write over TCP rather than to
model which fields can be trusted.

## Frame format

```
FF 55 <LEN> <OPCODE> [<OPERAND>]
```

No terminator. No checksum. `LEN` is `01` for amplifier-wide commands (opcode only) and
`02` for scoped ones (opcode plus one operand). Groups are **0-based**: A=`0x00` … H=`0x07`.

## Replies

**Exactly 50 bytes, NUL-padded, no terminator.** The vendor docs say space-padded; the
device sends `\0`. There is nothing to `readline()` on — a line-oriented reader hangs
forever. Read exactly 50 bytes under a timeout.

Two reply shapes, and they differ in whitespace:

```
query reply : "Cmd:Volume      ,Group:D Vol=-27 db"    6 spaces, space before db
command echo: "Cmd:VolumeUP   ,Group:D Vol=-27db"      3 spaces, NO space before db
```

So match `Vol=(-?\d{1,2})\s*db`, never a literal `" db"`. An absolute volume set echoes as
`VolumeUP` regardless of direction — the `Cmd:` label does not tell you what was sent.

**An empty reply means the group has no channels.** That is the zone-enumeration mechanism,
and it is only trustworthy on a settled persistent connection.

## Connection behaviour

Three findings that are not in any vendor document and that dictate the client design:

1. **One TCP session only.** With two sockets open concurrently, socket 1 received *both*
   replies and socket 2 received nothing. A second connection does not merely fail — it
   silently corrupts the first socket's reply stream. Hold exactly one connection for the
   config entry's lifetime.
2. **A single socket pipelines correctly.** Three queries down one connection returned three
   ordered 50-byte replies. FIFO future-correlation is sound.
3. **Connection churn drops replies.** Rapid connect-query-disconnect cycles produced
   missing and all-NUL responses, reproducibly. Another reason for one persistent socket,
   and a reason not to trust "empty reply" on a freshly opened one.

## Volume

`byte = dB + 183`, range −70 … +12 dB in 1 dB steps.

| dB | byte |
|---|---|
| −70 | `0x71` |
| −27 | `0x9C` |
| 0 | `0xB7` |
| +12 | `0xC3` |

**Absolute set is verified working on V2.2.8130**, despite the spreadsheet documenting it
only for V2.51. Tested 2026-09-20 on group D with nothing connected and the amp in standby:

| Sent | Read back |
|---|---|
| `FF 55 02 8F 03` (−40) | `Vol=-40 db` ✅ |
| `FF 55 02 80 03` (−55) | `Vol=-55 db` ✅ |
| `FF 55 02 71 03` (−70) | `Vol=-70 db` ✅ |
| `FF 55 02 9C 03` (−27) | `Vol=-27 db` ✅ |

No stepping fallback is needed.

## Command reference

`<N>` is a group, `0x00`–`0x07`. `<C>` is a channel, `0x08`–`0x0F`.

```
SET VOLUME    group    FF 55 02 <dB+183> <N>        all groups  FF 55 01 <dB+183>
GET VOLUME             FF 55 02 10 <N>
VOLUME UP / DOWN 1 dB  FF 55 02 04|05 <N>
VOLUME UP / DOWN 3 dB  FF 55 02 0E|0F <N>
RECALL TURN-ON VOLUME  FF 55 02 0D <N>

MUTE toggle/on/off     FF 55 02 06|07|08 <N>        all groups  FF 55 01 06|07|08
GET MUTE               FF 55 02 12 <N>

SOURCE 1-4             FF 55 02 <08+S> <N>          all groups  FF 55 01 <08+S>
GET SOURCE             FF 55 02 11 <N>

GROUP POWER on/off/tog FF 55 02 65|66|67 <N>        all groups  FF 55 01 65|66|67
GROUP POWER QUERY      -- does not exist; use HTTP page=status

AMP POWER on/off/tog   FF 55 01 01|02|03
AMP POWER QUERY        FF 55 01 70        -> "Power status :On"

GET DSP PRESET         FF 55 02 16 <C>
GET SHORT PROTECT      FF 55 02 17 <C>
GET OVERTEMP           FF 55 02 18 <C>
```

## ⛔ Forbidden: channel→group assignment

**Opcodes `0x21`–`0x28`** reassign channels between groups (`0x21`→A, `0x22`→B, …).

They are destructive, there is no safe inverse without a prior backup, and — critically —
**the echo lies.** Sending `FF 55 02 22 0A` (assign channel 2L to group B) returned
`Channel <name> group is B` for a change that never applied; the authoritative
`output-groups` still read `a` for that channel afterwards. The vendor's
own HTTP write endpoint failed the same way. Both appear to be refused while the amp is in
standby, while still reporting success.

This integration must never send these opcodes, and no service may surface them. Group
topology is configured in the amp's web UI at `http://<amp>/BasicSetting.htm`.

**Generalise the lesson: never treat this device's echo as confirmation of a config write.**
Read back over HTTP instead.

## Timing

From the Savant profile's own inter-command delays, not measured here:

- ~5 ms between consecutive commands
- 100–200 ms after a query before the next command
- 1000 ms after power-on before querying volume

The vendor's power-on sequence is amp on → group on (200 ms) → query volume (1000 ms),
rather than a bare group-on. Their power-off sends group-off **twice**, which suggests a
single one proved unreliable.

## Push: tested, and it does not

**The amplifier sends nothing unsolicited when state is changed out of band.** Tested
2026-09-20 on a DSP 8-130 MKII, firmware V2.2.8130:

```
idle baseline, 20s                      0 unsolicited frames
out-of-band volume change over HTTP     0 unsolicited frames
  (applied: output-volumes 6,7 -> -45)
control query on the same socket        1 frame, 10 ms
```

The control step is the part that makes it a result rather than an absence. Without it,
"zero frames" is indistinguishable from a reader that was never working — the query came
back on the same socket that had just sat silent for forty seconds, so the socket was alive
throughout and the silence was the amplifier's.

The change was driven over HTTP and verified applied *before* the listening window, so
nothing sent on the socket could be mistaken for a push.

⚠️ **One vector remains untested: audio sense.** The sibling Triad AMS integration handles an
unsolicited `AudioSense:Input[N]` frame, and this amplifier has the same sensing hardware —
its `auto-on-method` is `Audio`. Triggering it needs audio to start or stop on an input.
It would not change the design: an audio-sense event says a source woke up, not what the
volume is, so state would still be polled.

## Still unverified

- Behaviour on a malformed frame or an out-of-range volume byte. No NAK format is documented
  anywhere.
- Whether a group power change over TCP is reflected in the HTTP status page, and how fast.
- Whether the amp drops a held-open idle socket, and so whether a keepalive is needed.

## Model coverage

The DSP2-150 and DSP2-750 use the same protocol with two differences: **2 sources instead
of 4**, and **group A only**. A client covering all three needs only those two parameters.

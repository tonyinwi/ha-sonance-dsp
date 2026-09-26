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
GET /Web/Handler.php?page=in-out-settings&action=read
GET /Web/Handler.php?page=general-settings&action=read
```

⚠️ **Use `in-out-settings`, not `basicsettings`.** Both exist and both answer, which is what
makes this worth writing down: `basicsettings` is an older, cut-down view of the same data
and returns **none** of `turn-on-volumes`, `maximum-volumes`, `gain-offset`, `level-trim-dBs`,
`stereo-or-mono`, `mode-sources` or `sources-2`. A wrong-but-working endpoint is harder to
notice than a broken one.

It is also the harder one to find. `Landing.htm` links only `BasicSetting.htm` and
`GeneralSettings.htm`; the In/Out and EQ tabs are linked from *inside* `GeneralSettings.htm`,
so following the landing page alone leads to the lesser endpoint.

`status` returns per-group power and mute:

```json
{"status-titles":["GROUP A", ...],
 "power-status":["on","on","on","on","off","off","off","off"],
 "mute-volumes":["off","off","off","off","off","off","off","off"]}
```

`in-out-settings` returns the zone topology and every per-channel setting:

```json
{"output-names":["Zone 1 L","Zone 1 R","Zone 2 L", ...],
 "input-names":["Streamer L Digital","Streamer R Digital","Input 2L", ...],
 "output-groups":["a","a","b","b","c","c","d","d"],
 "dsp-presets":[44,44,47,47,48,48,0,0],
 "output-volumes":["-27","-27", ...]}
```

Every list is indexed by channel, 1L through 4R. Beyond the fields above it also returns
`turn-on-volumes`, `maximum-volumes`, `gain-offset`, `level-trim-dBs`, `stereo-or-mono`,
`bridge-modes`, `mode-sources` and `sources-2`.

Three of those change how the device should be driven:

- **`maximum-volumes`** is the amplifier's *own* per-channel ceiling. An integration's own
  volume limit cannot raise a zone past it, and a group is only as loud as its most
  restricted channel.
- **`gain-offset`** is installer calibration, and it means **a dB figure is not comparable
  between zones**: two zones both reading −27 dB with offsets of −6 and +4 are ten dB apart
  in practice. Anything that averages zone volumes has to say so.
- **`sources-1` / `sources-2`** are indices into `input-names`, giving each output channel's
  two assignable source slots. `mode-sources` toggles the second. This is an *assignment*,
  not a selection — which slot is live comes from the TCP source query.

Note the UI's "Source 1 / Source 2" rows are a different concept from the TCP `source 1–4`
commands: the former are the two assignable slots per channel, the latter select among the
four physical input pairs.

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
GET SOURCE             FF 55 02 11 <N>    -- see the two traps below

GROUP POWER on/off/tog FF 55 02 65|66|67 <N>        all groups  FF 55 01 65|66|67
GROUP POWER QUERY      -- does not exist; use HTTP page=status (see Power)

AMP POWER on/off/tog   FF 55 01 01|02|03
AMP POWER QUERY        FF 55 01 70        -> "Power status :On"

GET DSP PRESET         FF 55 02 16 <C>
GET SHORT PROTECT      FF 55 02 17 <C>
GET OVERTEMP           FF 55 02 18 <C>
```

## Source: two traps, both measured

Captured on group D (no speakers connected) on 2026-09-20.

### `Src1=` names the Source 1 slot, not a source number

```
select source 2  ->  query answers  'Cmd:Source1     ,Group:D Src1=Input 2L'
select source 3  ->  query answers  'Cmd:Source1     ,Group:D Src1=Sonos L Analog'
select source 4  ->  query answers  'Cmd:Source1     ,Group:D Src1=Input 4L'
```

**Both `Cmd:Source1` and `Src1=` stay `1` whatever is selected.** Only the name changes. A
parser that reads the digit as the source number gets `1` forever.

The reason is in the manual's terminology rather than a firmware quirk. Each output
channel has two source *slots*, **Source 1** (the routed source) and **Source 2** (an
override for paging or a doorbell, see below). The query reports which input is assigned
to **slot 1**, so the `1` is the slot, and it was never meant to be a source number. An
earlier version of this document called it a "fixed label"; the behaviour described was
right, the explanation was not.

Resolve the source from the **name** instead, through `input-names` from the HTTP endpoint.
Inputs are stereo pairs, so indices 0/1 are source 1, 2/3 are source 2, and so on; a group
query reports its LEFT member, so the name is normally the even index of the pair.

### Source 2 and Mode Source 2: a hardware override layer

Not used by this integration, recorded because it is easy to mistake for routing. Per
channel, **Source 2** is a second input and **Mode Source 2** decides what it does
(MkIII manual, In/Out Settings):

- `OFF` -- *"Source 2 has no effect on the operation of the channel."*
- `MIX` -- *"Input levels will be attenuated by 6dB, and signals will be summed."*
- `MUTE` -- *"Source 1 will be muted while Source 2 is active."* Audio-sensed ducking,
  intended for a doorbell or paging input.

Readable over HTTP as `sources-2` and `mode-sources`; no TCP opcode for either is known.

### A source *change* replies with 256 bytes, not 50

```
SET source 2  ->  256B:  [0]      'Cmd:Source2     , Group:D'
                         [50..255] all NUL
SET source 1  ->   50B:  [0]      'Cmd:Source1     , Group:D'
```

The payload is in the first 50 bytes; the rest is padding. Opcodes `0x0A`/`0x0B`/`0x0C` pad
to 256, `0x09` does not — reproducible across repeats.

A client reading a fixed 50 bytes takes the first frame and leaves **206 NUL bytes queued**,
so the next four commands read pure padding and look like "no reply" before the fifth
resynchronises. Any client that sends a source change must drain to the end of the frame.

This is the same class of problem the sibling Triad AMS integration solves with an adaptive
drain — its comments describe firmware that pads to 150 bytes and firmware that terminates
with a single NUL. The variation is not only across firmware revisions: on this amplifier it
varies **by opcode within one firmware**.

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

## Power: measured in Power Button mode

Everything here was measured on 2026-09-26, silently, with the source idle. It
assumes the amplifier's **Auto On method is Power Button with every channel's
sleep set to OFF**, which is the vendor's own recommendation for IP control
(*"When controlling the amplifier using IP and IR commands we suggest using the
Power Button Auto On mode."*). In `Audio` mode the amplifier wakes zones on
signal by itself, and in `Audio Green` it also drops the network while asleep.

| Command | Reply | Notes |
|---|---|---|
| `FF 55 02 65 <N>` zone on | `Cmd:GroupON ,Group:X` | one command is enough |
| `FF 55 02 66 <N>` zone off | `Cmd:GroupOFF ,Group:X` | the Savant profile sends this twice; not needed here |
| `FF 55 01 01` amp on | `Cmd:PowerOn` | starts a ~10 s boot |
| `FF 55 01 02` amp standby | `Cmd:PowerOff` | network stays up |
| `FF 55 01 70` amp query | `Power status :On` / `:Off` | the only master-power read |

What each command actually does:

- **Switching a zone on clears its mute**, and applies the zone's turn-on
  volume. The clear was already visible at the first sample, about 0.5 s after
  the zone-on, and a mute sent about 0.5 s after the zone-on stuck.

  ⚠️ *Corrected 2026-09-26.* This line used to say "re-muting immediately
  afterwards sticks". The probe behind it waited out a 0.5 s receive timeout
  after every command, so its "immediate" mute went out half a second later,
  after the clear had landed. Whether a mute sent within one round trip of the
  zone-on survives the clear is **unmeasured**. The integration reads the mute
  back across the window and re-sends it.
- **Standby and wake do not clear mute.** Zones muted before standby are still
  muted after it.
- **A switched-off zone still answers.** Volume, mute and source are all
  readable with the zone off -- so "it answered" does not mean "it is on".
- **A zone's on/off flag survives standby.** The HTTP status page reports a
  zone `on` while the whole amplifier is in standby. A zone is only producing
  output when the amplifier is on **and** the zone is on.
- **Switching every zone off does not put the amplifier in standby.** Master
  power stayed `On` with all four zones off.
- **Standby keeps the network up.** TCP and HTTP both answered with master
  power `Off`, so a controller can always wake it.
- **A wake takes about 10 s** (10.6 s measured; the manual says 9-12), and
  **mute writes sent before it finished were lost even though they echoed
  success.** Zone-on writes sent in the same window *were* applied. Why the two
  differed was not established -- one explanation that fits is that the mute
  clear lands when a zone actually powers up, over any mute sent before it.
  The integration's policy is the conservative one: nothing but status queries
  until the amplifier reports `On`.
  Status queries during the boot are fine.
- **The HTTP status page reflects a zone power change in 0.01-0.06 s**, which
  makes it a reliable read-back.

These were **not** measured, and the integration is written to be right
either way rather than to depend on them:

- **How soon after a zone-on a mute survives.** See the correction above. The
  mute is read back at 0.3, 0.6, 1.0 and 1.5 s and re-sent whenever it reads
  off, and the log records when that happens -- so the first time it does, the
  answer is in the log.
- **Power-on sent to an amplifier that is already on**, and **standby sent to
  one already in standby.** Power-on is only ever sent straight after a read of
  `Off`; if the amplifier's power cannot be read, nothing is switched.
- **A zone-on sent to a zone that is already on.** Only off-to-on was measured.
  If it re-applies the turn-on volume and clears mute the way off-to-on does, a
  scene re-asserting "on" would reset a playing zone. So it is never sent to a
  zone the amplifier reports on.
- **Zone commands while the amplifier is in standby.** Volume writes in standby
  were measured to work (2026-09-20); zone on/off was not. Waking brings back
  every zone whose flag survived standby, so before a wake the integration
  switches the other flagged zones off, then again after it in case standby
  ignored or did not answer the first attempt. Its debug log says which it was
  -- the status page shows the flags in standby -- so this answers itself the
  first time it happens.

### Turn-on volume

Per zone, in the In/Out tab: either a fixed level, or `LAST` to keep the volume
across a power cycle. A zone power-on over IP applies it -- the manual only
mentions the power switch and sleep. The web UI appears to store `LAST` as the
out-of-range value `13`; that is inferred from its JavaScript and unverified.

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
- Whether the amp drops a held-open idle socket, and so whether a keepalive is needed.
- A zone-on sent to a zone that is already on, and zone on/off sent in standby. See
  *Power* above: the integration avoids depending on either.

(Whether a zone power change over TCP shows on the HTTP status page, and how fast, was on
this list until 2026-09-26: it does, in 0.01-0.06 s.)

## Model coverage

The DSP2-150 and DSP2-750 use the same protocol with two differences: **2 sources instead
of 4**, and **group A only**. A client covering all three needs only those two parameters.

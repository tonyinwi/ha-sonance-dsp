# Sonance DSP protocol reference

What the device does, measured on a **DSP 8-130 MKII, firmware V2.2.8130**, on 2026-09-20
unless dated otherwise. Where the vendor spreadsheet (`DSP_IP_Codes_SONANCE.xlsx`) or the
Savant profile disagrees, the device wins and the contradiction is called out. Decisions:
[`design.md`](design.md).

## Transports

| | TCP 52000 | HTTP 80 |
|---|---|---|
| Format | binary frames | JSON |
| Set volume / mute / source / power | yes | not used (see *Writes*) |
| Serial, model, firmware | no | yes |
| Zone and source names | partial | yes |
| Per-group power | no, no such opcode | yes |
| Channel→group map | only "empty or not" | yes, authoritative |

### HTTP endpoints

Undocumented by the vendor; found in the web UI's JavaScript. JSON is served as
`text/html`.

```
GET /Web/Handler.php?page=status&action=read
GET /Web/Handler.php?page=in-out-settings&action=read
GET /Web/Handler.php?page=general-settings&action=read
```

**Use `in-out-settings`, not `basicsettings`.** Both answer, but `basicsettings` is an
older, cut-down view lacking `turn-on-volumes`, `maximum-volumes`, `gain-offset`,
`level-trim-dBs`, `stereo-or-mono`, `mode-sources` and `sources-2`. It is also the one
`Landing.htm` leads to; the In/Out tab is linked only from inside `GeneralSettings.htm`.

`status`: per-group power and mute.

```json
{"status-titles":["GROUP A", ...],
 "power-status":["on","on","on","on","off","off","off","off"],
 "mute-volumes":["off","off","off","off","off","off","off","off"]}
```

`in-out-settings`: zone topology and every per-channel setting, each list indexed by
channel, 1L through 4R.

```json
{"output-names":["Zone 1 L","Zone 1 R","Zone 2 L", ...],
 "input-names":["Streamer L Digital","Streamer R Digital","Input 2L", ...],
 "output-groups":["a","a","b","b","c","c","d","d"],
 "dsp-presets":[44,44,47,47,48,48,0,0],
 "output-volumes":["-27","-27", ...]}
```

It also returns `turn-on-volumes`, `maximum-volumes`, `gain-offset`, `level-trim-dBs`,
`stereo-or-mono`, `bridge-modes`, `mode-sources`, `sources-1` and `sources-2`. Three
change how the device should be driven:

- **`maximum-volumes`**: the amp's own per-channel ceiling. No integration setting can
  raise a zone past it, and a group is only as loud as its most restricted channel.
- **`gain-offset`**: installer calibration, so **dB is not comparable between zones**.
  Two zones at −27 dB with offsets of −6 and +4 are 10 dB apart.
- **`sources-1` / `sources-2`**: indices into `input-names`, each channel's two assignable
  source slots; `mode-sources` sets what slot 2 does. An assignment, not a selection: the
  TCP source query says which input is live. Distinct from the TCP `source 1–4` commands,
  which pick one of four input pairs.

`general-settings`: `serial-number`, `amplifier-name`, `amplifier-model`,
`firmware-version` and network config. The serial is the stable identity; these amps are
usually on DHCP.

### Writes

An `action=write` form exists. A `name=output-group` write returned unchanged JSON for a
change it did not apply (amp in standby); a `name=output-volume` write applied reliably
(during the push test). One field misbehaves, not the endpoint. The integration writes
only over TCP.

## Frame format

```
FF 55 <LEN> <OPCODE> [<OPERAND>]
```

No terminator, no checksum. `LEN` is `01` for amplifier-wide commands (opcode only) and
`02` for scoped ones (opcode plus operand). Groups are **0-based**: A=`0x00` … H=`0x07`.

## Replies

**Exactly 50 bytes, NUL-padded (the vendor docs say space), no terminator.** `readline()`
hangs forever; read exactly 50 bytes under a timeout. Changes to sources 2–4 are the
exception (below).

Query replies and command echoes differ in whitespace:

```
query reply : "Cmd:Volume      ,Group:D Vol=-27 db"    6 spaces, space before db
command echo: "Cmd:VolumeUP   ,Group:D Vol=-27db"      3 spaces, NO space before db
```

Mute query: `Cmd:MuteState   ,Group:D Mute=on` (captured 2026-09-20).

Match `Vol=(-?\d{1,2})\s*db`, never a literal `" db"`. An absolute set echoes `VolumeUP`
whatever the direction, so `Cmd:` does not say what was sent. Scoped replies echo their
`Group:` letter, the only field that ties a reply to its request.

**No reply, or 50 NUL bytes, means the group has no channels**, which is how zones are
enumerated (on a settled connection only; see churn below).

## Connection behaviour

None of this is in the vendor docs.

1. **One TCP session only.** With two sockets open, socket 1 received *both* replies and
   socket 2 nothing. A second connection silently corrupts the first one's reply stream.
2. **One socket pipelines correctly.** Three queries returned three ordered replies. The
   client serialises anyway; see [`design.md`](design.md).
3. **Connection churn drops replies.** Rapid connect-query-disconnect cycles gave missing
   and all-NUL replies, reproducibly, so an empty reply on a fresh socket proves nothing.

## Volume

`byte = dB + 183`, −70 … +12 dB in 1 dB steps.

| dB | −70 | −27 | 0 | +12 |
|---|---|---|---|---|
| byte | `0x71` | `0x9C` | `0xB7` | `0xC3` |

**Absolute set works on V2.2.8130**, though the spreadsheet documents it only for V2.51.
On group D (nothing connected, amp in standby), sets to −40, −55, −70 and back to −27
(`FF 55 02 8F 03`, `80`, `71`, `9C`) each read back exactly. No stepping fallback is
needed.

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

Group D, no speakers connected.

### `Src1=` names the Source 1 slot, not a source number

```
select source 2  ->  query answers  'Cmd:Source1     ,Group:D Src1=Input 2L'
select source 3  ->  query answers  'Cmd:Source1     ,Group:D Src1=Sonos L Analog'
select source 4  ->  query answers  'Cmd:Source1     ,Group:D Src1=Input 4L'
```

**`Cmd:Source1` and `Src1=` stay `1` whatever is selected**; only the name changes. The
`1` is the manual's **Source 1** slot (the routed source; **Source 2** is an override,
below), and the query reports which input is in it.

Resolve the source by **name** through `input-names`. Inputs are stereo pairs (indices 0/1
are source 1, 2/3 source 2, …) and a group query reports its left member, so the name is
normally the even index.

### Source 2 and Mode Source 2: a hardware override

Not used here; easy to mistake for routing. Per channel, **Source 2** is a
second input and **Mode Source 2** sets what it does (MKIII manual, In/Out Settings):

- `OFF`: no effect on the channel.
- `MIX`: both inputs attenuated 6 dB and summed.
- `MUTE`: Source 1 muted while Source 2 is active. Audio-sensed ducking for a doorbell or
  paging input.

Readable over HTTP as `sources-2` and `mode-sources`; no TCP opcode for either is known.

### A source *change* replies with 256 bytes, not 50

```
SET source 2  ->  256B:  [0]      'Cmd:Source2     , Group:D'
                         [50..255] all NUL
SET source 1  ->   50B:  [0]      'Cmd:Source1     , Group:D'
```

Payload in the first 50 bytes, NUL after, all in one TCP segment. Opcodes
`0x0A`/`0x0B`/`0x0C` pad to 256 and `0x09` does not, reproducibly.

Reading a fixed 50 leaves **206 NUL bytes queued**: the next four commands read padding
and look like "no reply", and the fifth resynchronises. Drain to the end of the frame after
every source change.

The sibling Triad AMS integration drains adaptively because its firmware revisions differ
(150-byte padding, or one NUL). Here it varies **by opcode within one firmware**.

## Forbidden: channel→group assignment

**Opcodes `0x21`–`0x28`** reassign channels between groups (`0x21`→A, `0x22`→B, …).

They are destructive, have no safe inverse without a prior backup, and **the echo lies**:
`FF 55 02 22 0A` (channel 2L to group B) returned `Channel <name> group is B`, yet
`output-groups` still read `a` for that channel. The HTTP `output-group` write failed the
same way (*Writes*, above). Both appear to be refused in standby while reporting success.

The integration never sends them ([Forbidden operations](design.md#forbidden-operations)).
Group topology is set in the amp's web UI, `http://<amp>/BasicSetting.htm`.

## Timing

From the Savant profile, not measured:

- ~5 ms between consecutive commands
- 100–200 ms after a query before the next command
- 1000 ms after power-on before querying volume

Savant powers on as amp on → group on (200 ms) → query volume (1000 ms), and powers off
by sending group-off **twice**.

## Power: measured in Power Button mode

Measured 2026-09-26, silently, source idle, with the amp's **Auto On method set to Power
Button and every channel's sleep OFF**: the vendor's advice for IP and IR control. In
`Audio` mode the amp wakes zones on signal by itself; in `Audio Green` it also drops the
network while asleep.

Replies verbatim, padding included:

| Command | Reply | Notes |
|---|---|---|
| `FF 55 02 65 <N>` zone on | `Cmd:GroupON      ,Group:X` | one is enough |
| `FF 55 02 66 <N>` zone off | `Cmd:GroupOFF      ,Group:X` | one is enough (Savant sends two) |
| `FF 55 02 07 <N>` mute on | `Cmd:MuteOn      , Group:X` | space after the comma |
| `FF 55 02 08 <N>` mute off | `Cmd:MuteOff     , Group:X` | |
| `FF 55 01 01` amp on | `Cmd:PowerOn` | starts a ~10 s boot |
| `FF 55 01 02` amp standby | `Cmd:PowerOff` | network stays up |
| `FF 55 01 70` amp query | `Power status :On` / `:Off` | the only master-power read |

Only `On` and `Off` have been seen from the amp query. The integration reads any other
word there, and any status-page value but `on`/`off`, as unknown, never off.

Measured:

- **A zone-on has an audible power-up window** (2026-09-27, zone D, 50 ms sampling, four
  runs; heard by ear with a source playing):

  | After zone-on | The amp |
  |---|---|
  | ~0.2 s | applies the zone's **turn-on volume**, over any volume sent before it, and reports **mute off** |
  | ~0.2–1.05 s | **plays unmuted at the turn-on volume**; a mute sent now changes nothing |
  | ~1.05 s | re-applies a mute sent with the zone-on, without being asked again |

  With the turn-on volume at −70 dB the same window was **inaudible** (by ear). Seen on
  zones A–D, awake and straight after a wake.

  *Corrected 2026-09-27:* PR #13 recorded "a mute sent straight after a zone-on survives"
  from one sample, a zone off for only a few seconds; every later run showed the window.
  *Corrected 2026-09-26:* an earlier "re-muting immediately sticks" came from a probe that
  waited 0.5 s per command.
- **Standby and wake do not clear mute.**
- **A switched-off zone still answers** volume, mute and source queries, so answering does
  not mean on.
- **A zone's on/off flag survives standby.** The status page reports a zone `on` while the
  amp is in standby. A zone plays only when the amp **and** the zone are on.
- **Switching every zone off does not put the amp in standby.** Master power stayed `On`.
- **Standby keeps the network up.** TCP and HTTP both answered with master power `Off`.
- **A wake takes about 10 s** (10.6 s measured; manual: 9–12). **Mute writes sent
  during it were lost although they echoed success**; zone-on writes in the same window
  were applied. Cause unknown; one fit is that the mute clear lands when a zone actually
  powers up. Status queries during the boot are fine.
- **The HTTP status page reflects a zone power change in 0.01–0.06 s**, a reliable
  read-back.

Not measured; the integration is built to be right either way
([Power](design.md#power)):

- **Whether a wake changes zone volumes.** The integration reads a zone's level before waking.
- **Power-on to an amp already on**, and **standby to one already in standby.**
- **Zone-on to a zone already on.** Only off-to-on was measured. If it too re-applies the
  turn-on volume and clears mute, a scene re-asserting "on" would reset a playing zone.
- **Zone on/off in standby.** Volume writes in standby work (2026-09-20). The status
  page shows zone flags in standby, and the integration logs any flag a zone-off left set.

### Turn-on volume

Per zone, in the In/Out tab: a fixed level, or `LAST` to keep the volume across a power
cycle. A zone power-on over IP applies it, about 0.2 s after the zone-on (above); the
manual mentions only the power switch and sleep. Set over HTTP as `in-out-settings`
`action=write&name=turn-on-volume&index=<channel>&value=<dB>`; read back to confirm. The web UI appears to store `LAST` as the out-of-range value `13` (inferred from its
JavaScript, unverified).

## Push: tested, and it does not

**The amplifier sends nothing unsolicited when state changes out of band.**

```
idle baseline, 20s                      0 unsolicited frames
out-of-band volume change over HTTP     0 unsolicited frames
  (applied: output-volumes 6,7 -> -45)
control query on the same socket        1 frame, 10 ms
```

The control query makes this a result rather than an absence: it came back on a socket
that had sat silent for forty seconds, so the silence was the amplifier's. The change went
over HTTP and was verified *before* listening began, so no socket traffic could pass for a
push.

**Untested: audio sense.** The sibling Triad AMS integration handles an unsolicited
`AudioSense:Input[N]` frame, and this amp has the same sensing hardware (its
`auto-on-method` was `Audio` when push was tested). Triggering it needs audio to start or
stop on an input.

## Still unverified

- The reply to a malformed frame or out-of-range volume byte. No NAK format is documented.
- Whether the amp drops an idle held-open socket, and so whether a keepalive is needed.
- The unmeasured power cases under *Power*.

## Other models

The DSP 2-150 and DSP 2-750 use the same protocol with **2 sources instead of 4** and
**group A only**. Untested.

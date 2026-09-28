# Design

Decisions and why. Device facts and their evidence are in [`protocol.md`](protocol.md);
most of what follows is forced by them rather than chosen.

## Scope

Local control of a Sonance DSP amplifier: per-zone volume, mute, source and power as
`media_player` entities, with zones discovered from the device.

**Non-goals:**

- **Audio streaming.** The amplifier has only line inputs, no network audio. It cannot be
  a Music Assistant or Squeezelite player.
- **DSP tuning.** Crossovers, EQ and speaker presets belong in the amplifier's web UI, set
  by someone who knows the speakers. A preset list in an automation engine invites a
  mistake with a physical consequence.
- **Channel-to-group assignment.** See [Forbidden operations](#forbidden-operations).

## Two transports

**TCP 52000 for writes and fast state. HTTP 80 for identity, names, the channel map and
zone power**; TCP cannot read identity or zone power at all. What each port can do is in
[`protocol.md`](protocol.md).

The HTTP JSON API is undocumented (found in the web UI's JavaScript), so a firmware update
could move it. Setup needs it, because the serial is the identity. The channel map is read
once, at setup; if that read fails, zones get generic names and no source list. After
setup, losing HTTP costs only zone power, never volume control.

HTTP is read-only by choice. Its `action=write` form applies some fields and not others
(an `output-group` write reported success without applying), and TCP is where a reply can
be matched to its request. Read over HTTP and write over TCP, rather than track which
fields can be trusted.

## Connection model

- **One TCP session for the config entry's lifetime.** A second concurrent socket corrupts
  the first one's reply stream: wrong values, no error. So no pool, no
  connect-per-command, and no other controller (Savant, Control4, RTI, Crestron) on the
  same amplifier: that is the hardware.
- **Serialised by a lock, not pipelined.** Replies carry no request id; the Nth reply
  belongs to the Nth command. The device pipelines correctly, but a queue of futures falls
  permanently one behind the first time a command gets no reply, and an empty group
  answers with silence, so that is routine. The lock costs a round trip per command and
  makes a timeout local instead of corrupting. An unexpected timeout drops the socket:
  reconnecting is the only certain realignment.
- **Fixed 50-byte reads, never `readline()`.** Replies have no terminator.
- **The group letter is checked.** Scoped replies echo `Group:`, the only proof a reply
  belongs to its query. Unchecked, one slow group makes zone A vanish and invents a phantom
  zone C. Getters raise on a mismatch, giving a failed poll and a clean reconnect.
  Discovery discards the stale reply and carries on instead: the HTTP cross-check restores
  anything missed, and failing setup over one slow reply would be worse.
- **A failed or cancelled request tears the socket down**, including cancellation from
  outside, which `asyncio.timeout` does not convert and which Home Assistant raises when
  it cancels a refresh on reload. `readexactly` keeps a partial read buffered, and the next
  command would read it.
- **Close is final, even mid-connect.** `disconnect()` is lock-free, because a request
  tears down from inside the lock and `asyncio.Lock` is not reentrant. So it can land while
  a connect is suspended in `open_connection`, and the connect re-checks the closed flag
  and discards the new socket. Otherwise that socket is orphaned, and on a one-session
  device it locks out every later setup until Home Assistant restarts.
- **Reconnection is lazy.** The next command reopens a torn-down socket, so the retry
  cadence is the poll interval and the coordinator's backoff covers repeated failure. A
  second backoff loop would fight it.
- **Availability follows the coordinator's last update, not the socket.** A late reply
  tears the socket down, so keying on it would mark every zone unavailable for an
  interval.

## Polling, not push

`iot_class` is `local_polling` because the amplifier sends nothing unsolicited: tested,
not inferred (see [`protocol.md`](protocol.md)). The untested vector, audio sense, would
not change this: it says a source woke up, not what the volume is.

## Entity model

**One device** (the amplifier, keyed by serial) with one entity per zone. Zones are
groups in one box with one serial, firmware and address; a device per zone would invent a
hierarchy the hardware lacks.

### Zone entities

One `media_player` per populated group, named from the device: member channel names with
the L/R suffix stripped (`Patio L` + `Patio R` → **Patio**), or `Zone A` without the
channel map.

Zones are **discovered, not configured**: query volume on groups `0x00`–`0x07`, where
silence means no channels, then cross-check against the HTTP channel map. The map wins on
disagreement, because connection churn produces the same empty replies. Discovery runs
once, at setup; see [Known limitations](#known-limitations).

### The zone is the player; there is no amp-level entity

An amp-level entity, then a per-source one, were designed to give Music Assistant one
volume to hold when a streamer feeds several zones. Both are dropped. As in Control4 room
audio, **each zone is an independent player** with its own source, volume, mute and power,
and zones on one source share its playback.

That fits the standard `media_player`: source selection routes the amp, power switches the
zone, mirroring shows the source's track, and transport and media pass to the source's player;
`media_player.join` fits the same model ([roadmap](roadmap.md#grouping)).

**Transport and media only while the zone is on.** Play, pause, stop, play media and
browse go to the linked player, and the zone offers those the player does. An off zone
offers none: playback belongs to the source, so "play" there would start every other zone
on it and leave this one silent. Home Assistant rejects a control an entity does not offer,
so the integration's own refusal only catches a call queued before the zone went off.
Browse has no such gate, so the zone refuses it itself.

- **No skip.** Every zone on a source would forward it, so one "next track" sent to the
  streamer and its zones (Assist, an area, a group) would skip once per player.
- **No search.** Assist's search-and-play needs one target; a zone beside its player makes
  "play X in \<area\>" ambiguous.
- **No announcements.** Dropped with a warning, never passed on: on the source one would
  sound in every zone on it. Dropped rather than refused, because an announcement to an
  area, floor or label holding a zone that is on would otherwise fail as a whole after the
  player itself had spoken. Text-to-speech is also caught by its id, because a universal
  player strips the announce flag; a plain chime sent through one arrives as ordinary
  media. Send announcements to the player.
- **Media is the player's.** Browse returns its tree unchanged, thumbnails included; ids
  pass through unresolved.
- **One call, one forward.** Zones in one call on one source forward once, keyed on the
  call's context and the request until the forward returns. A call that reaches the player
  and its zones, by name, area, floor or label, reaches the player twice.
- **Loops are dropped.** A link into this integration is not followed. A forward that comes
  back through another player, such as a group containing the zone, carries the context
  the zone gave it, and is dropped. Browse carries no context, so a call-path guard
  refuses it instead; transport and play do not use that guard, because tasks inherit it
  and a dropped pause fails open.
- **Muted is not off.** A muted zone offers play and play media: mute is a listening
  control on a zone that is on, and the source-wide effect is the rule above.
- **Scenes and toggles act on the source.** Zones do not report `media_content_id`, so a
  restore resumes or pauses the source and never replays a track. Scenes saved with one
  before 0.5.0 still replay it. Play/pause toggled on two zones at once can cancel out.
- **An explicit target that is off fails the whole call**, as for any entity without the
  feature.
- **Features follow power**, so each zone on/off rewrites the entity registry and reloads
  the zone's HomeKit accessory. Expected, not a bug.

There is no second kind of entity, and no "volume" averaged from other volumes, which
[`gain-offset`](protocol.md#http-endpoints) would make meaningless.

The one constraint left: **playback belongs to the source, not the zone.** Pausing one zone
on a streamer pauses every zone on it.

### Feature flags, and why they are not cosmetic

Device class `RECEIVER`, with `VOLUME_SET`, `VOLUME_STEP`, `VOLUME_MUTE`, `SELECT_SOURCE`,
`TURN_ON` and `TURN_OFF`. `RECEIVER` and `VOLUME_STEP` are a trade, because HomeKit Bridge
routes `media_player` by device class:

| Device class | HomeKit result |
|---|---|
| `RECEIVER` | Receiver accessory, the only route with a real volume control. Its speaker service needs `VOLUME_MUTE` or `VOLUME_STEP`; `VOLUME_SET` alone yields nothing. |
| `SPEAKER` or unset | A mute-only switch with no volume, or nothing at all without `VOLUME_MUTE`. |

The cost of `RECEIVER`: Home Assistant makes `TV`/`RECEIVER`/`PROJECTOR` accessory-mode
only, so a bridge created in the UI **silently excludes** these zones. Each needs its own
HomeKit instance, as every AVR integration does. That buys a volume slider; `SPEAKER`
would buy bridging with no volume, the wrong half for an amplifier. Alexa and Assist need
only `VOLUME_SET`.

### Why not `number` entities for volume

A `number` gains long-term statistics but loses the media control card, Assist's volume
intents, Alexa's Speaker interface and HomeKit. If volume history matters, add a
diagnostic `number` alongside the `media_player`.

## Known limitations

- **The channel map is read once, at setup**: zones, names, sources and the amplifier's
  own ceilings. A change in the web UI needs a reload.
- **A wake from outside Home Assistant** brings back every zone whose flag survived
  standby. A wake from Home Assistant does not; see [Power](#power).
- **An unexpected reply format** is logged with its raw text rather than dropped, so a
  firmware difference becomes a bug report rather than a dead control.

## Volume

Range and encoding are in [`protocol.md`](protocol.md). Home Assistant's 0–1 maps onto
−70 dB up to a **ceiling set in the options flow, 0 dB by default**. It is one value for
every zone, lowered per zone to the amplifier's own
[`maximum-volumes`](protocol.md#http-endpoints) for that group (flagged by
`max_volume_db_capped_by_device`).

+12 dB is not the default: it is the factory turn-on level, and the vendor's integrator
notes flag it. A slider whose right end is maximum gain will eventually be dragged there
by a phone in a pocket. Raising the ceiling is a deliberate act.

Volume up and down move one device step (1 dB), not Home Assistant's default 10%.

## Power

Home Assistant owns power. The amplifier's Auto On method must be **Power Button** with
every channel's sleep **OFF**, so nothing changes power by itself. In `Audio` mode a zone
wakes the moment its source plays, and a zone unmuted by accident at 2am plays into the
garden. Those two settings and every zone's −70 dB turn-on volume are read at setup and
daily, each with a repair issue while wrong (`checks.py`): a factory reset or a web-UI
change undoes them silently, and the integration cannot set them itself.

The rules rest on [Power: measured in Power Button mode](protocol.md#power-measured-in-power-button-mode),
including what was not measured.

- **A zone is on only when the amplifier and the zone are both on.** Zone flags survive
  standby, so either alone lies. Off if either reads off, else unknown if either is
  unknown, never `on`: a switched-off zone still answers queries. An unknown zone does not
  mirror its source's track.
- **One missed read does not make a zone unknown.** HomeKit shows unknown as off, and
  `media_player.toggle` answers unknown by switching the zone *off*. So zone power from
  the status page is kept for five minutes (or three poll intervals, if longer), and an
  amplifier "on" across up to two unanswered queries. "Standby" is never carried: if something
  else woke the amp, zones would show off while playing. An empty or malformed status page
  counts as no answer; an empty map reads as "every zone off", and would put the amp in
  standby under playing zones.
- **Switching on is silent only with a −70 dB turn-on volume.** For about a second after a
  zone-on the amp plays unmuted at the zone's turn-on volume, and no command changes that
  ([protocol](protocol.md#power-measured-in-power-button-mode)). So every zone's turn-on
  volume is set to −70 dB on the amp, and once the window has passed the level the zone
  had while off is put back, capped at the ceiling and checked by read-back. That level is
  read from the amp (an off zone reports it), so it survives a restart. A restore that
  fails leaves the zone on at −70, and says so. **A muted zone is not restored**: any
  volume change un-mutes a zone on this amp, so its level is held instead (below).
- **An off zone takes no volume.** The amp keeps a level sent to an off zone, and switching
  it on restores that level, so a slider nudged while off would set the next switch-on
  level, unheard until then. Volume set and step are refused while the zone is known to be
  off (zone off, or amp in standby), checked inside the command lock. They stay allowed
  when power is unknown: turning a playing zone down must not depend on the status page.
  The features stay advertised; taking them away while off would change the zone's HomeKit
  accessory.
- **A muted zone is never sent a volume.** Any volume change un-mutes a zone, even an off
  one ([protocol](protocol.md#power-measured-in-power-button-mode)), so while a zone is
  muted, volume changes from HA are held and shown, not sent, and unmuting applies the
  held level: the volume set is itself the unmute. If the mute cannot be read (asked
  twice) the change is refused with an error rather than guessed. A zone switched on
  muted keeps its silent turn-on level with its own level held, and a hold survives off/on
  cycles; the restore after an unmuted switch-on re-reads the mute before each write. A
  failed unmute shows the mute as unknown, never "muted". A zone un-muted outside HA
  drops its hold. Holds, and owed mutes, are saved to Home Assistant's storage and
  reloaded at setup, so a restart keeps them; removing the entry deletes them.
- **Switching on restores the mute, and checks it stuck.** The mute is read while the zone
  is off (it keeps it there) and sent straight after the zone-on, both frames under one
  hold of the protocol lock. It is read back at 0.3, 0.6, 1.0 and 1.5 s (to 5 s after a
  wake) and re-sent on anything but a confirmed "muted", including no answer; the amp
  re-applies it at ~1.05 s. Without a confirmed read the switch-on has failed. If the
  pre-read fails, the last polled value is used; with none, the zone comes on muted.
- **A muted zone's switch-on that breaks part-way leaves it silent.** If a step after the
  zone-on fails (a lost echo, an unconfirmed mute), the zone is muted and checked again;
  failing that, switched off and muted once more while off. The mute is then *owed*: the
  next switch-on restores it whatever the amplifier reports, until a restore is confirmed
  or someone sets the mute. The status page decides the power shown, an unconfirmed mute
  shows unknown, and the error is still raised. A zone-on that never left (no connection)
  is the one failure that proves nothing happened, so it changes nothing. A caller
  cancelled before the zone-on cancels the request; after it, the sequence finishes and any
  failure is logged.
- **A zone that is already on is left alone.** Scenes and `homeassistant.turn_on` call
  `turn_on` without checking, and whether a zone-on resets a playing zone's volume is
  unmeasured. Judged from a fresh amplifier read and the status page, or the last known
  zone power when the page is down.
- **Unknown amplifier power is not guessed.** If it is unreadable after three tries,
  nothing is switched: power-on to an amp already on was never measured. Unknown *zone*
  power on an awake amp is different: a zone-on to a zone already on does nothing
  (measured), so the zone is simply switched on.
- **Waking brings back only the zone asked for.** A wake revives every zone that was on
  when something else put the amp to sleep, and all of them show off while it sleeps. So
  flagged zones, the requested one included, are switched off before the wake and again
  after it, whatever standby appeared to say (every zone, without the status page). The
  requested zone's zone-on is then always the measured off-to-on case, which applies its
  turn-on volume. If standby does not answer a zone-off, the rest wait for the wake rather
  than each timing out. A power-on with a lost echo is waited on anyway, so the clean-up
  runs. A revived zone that cannot be switched off shows on or unknown, never off, and the
  error is raised.
- **Everything waits for the boot.** From standby, turn-on sends power-on, then only status
  queries until the amplifier reports `On` (~10 s). Every write holds the coordinator's
  command lock, and a power change holds it throughout. `PARALLEL_UPDATES` is 0: Assist's
  relative-volume intent bypasses it anyway, and a limit of 1 would be held across
  transport calls to other players. Zones switched on together share one wake.
- **Volume steps start from a fresh read.** Up and down read the level inside the lock, so
  a step after a wake starts from the turn-on volume and a stale cache never turns a step
  into a jump. Without a reading the device's own step is sent, except upward when the last
  known level is already at the ceiling. Assist's percentage step works from `volume_level`
  before it reaches the lock, so a zone that is off, or unread since it was switched on,
  reports none and Home Assistant's handler errors instead of jumping.
- **A wake that times out is not retried for a minute**, or until a poll finds the amplifier on;
  the holdoff is checked before any write. Otherwise a scene switching four zones on
  against an amp that will not wake spends four timeouts, over a minute and a half, with
  every command queued behind them.
- **The status page is not waited on twice.** Inside a power change each read is bounded at
  three seconds (it answers in under a tenth of one), and after one failure the rest of the
  change does without it.
- **Polls do not overwrite what a command just set.** A poll does not start while a command
  holds the lock, and one already reading when a change lands discards its results. A
  skipped poll after a failed one still counts as failed.
- **One blip does not make a zone ignore `turn_off`.** Home Assistant silently skips an
  unavailable entity in a service call, so a failed poll asks again on a fresh connection
  before calling the amplifier unreachable, and the next poll after a failure comes in five
  seconds.
- **The last zone off puts the amplifier in standby**, which it does not do by itself. An
  unanswered zone-off is checked on the status page (it may have landed) and sent once
  more. Standby is sent whatever the cache says, since the cache can be stale. Without the
  status page another zone may still be on, so the amplifier is left alone. Turning off a
  zone in a sleeping amplifier is best effort; the status page decides what is shown, or
  unknown without it.
- **Power and mute writes are acknowledged by their own echo**, checked for command and
  group. Padding or another group's reply is a desync, not an acknowledgement. With the
  status page down, the echo is the only confirmation.
- **Power changes are confirmed on the status page**, and the command is retried once.
- **Unload waits for a power change in progress**, up to the wake timeout plus 15 s, and
  refuses anything queued. Cutting a wake off leaves the zones it revived playing.
- **Switching on applies the zone's turn-on volume**, fixed or `LAST`, so the volume is
  read back, asking twice.

Known limits, each needing an unmeasured behaviour or an unlikely combination:

- **"On but muted" in a scene plays briefly.** HA reproduces a scene as `turn_on`, then
  `volume_set`, then `volume_mute`, so a zone that starts off and unmuted comes on
  audibly before the mute arrives. Use a script: `volume_mute` first -- a mute sent to an
  off zone sticks -- then `turn_on`.
- **A scene passes through the zone's old level.** HA reproduces a scene as `turn_on` then
  `volume_set`, so a zone plays briefly at its restored level before the scene's own. In a
  script, call `volume_set` before `turn_on`: the restore reads the off zone's level.
- **Scene order affects latency.** Switching zone B, the last one playing, off before zone
  A on puts the amplifier in standby and then wakes it (~10 s). The end state is right.
  List the zones being switched on first.
- **A wake that fails part-way** skips the post-wake zone-offs. If standby also ignored the
  pre-wake ones and the boot then completes late, revived zones play. The error is raised
  and the next poll shows them on.
- **Home Assistant stopping mid-wake** is not waited for: stopping does not unload
  integrations. Unload and reload do.

## Forbidden operations

**Opcodes `0x21`–`0x28` reassign channels between groups. The integration must never send
them, and no service may surface them.**

They are destructive, have no safe inverse without a prior settings backup, and **the echo
lies**: it reports success for an assignment that never applied
([evidence](protocol.md#forbidden-channelgroup-assignment)). The frame builder refuses
everything in `FORBIDDEN_OPCODES` rather than leaving it to convention, because a mistake
here is invisible where it is made.

The wider rule: **this device's echo does not confirm a configuration change; read it back
over HTTP.** Group topology belongs in the amplifier's web UI.

## Error handling

- Writes surface protocol errors as translated `HomeAssistantError`s naming the entity and
  the operation, not raw socket errors.
- Connection loss is logged once going down and once coming back, not on every failed
  poll. An amplifier unreachable overnight should not produce hundreds of log lines.
- Setup failure raises `ConfigEntryNotReady`, so Home Assistant retries with backoff. That
  includes finding no zones, which an amplifier not yet answering also produces.

## Identity

The config entry's `unique_id` is the amplifier's **serial number**, read over HTTP. These
amplifiers are commonly on DHCP, and Home Assistant disallows IP, hostname and device name
as unique ids for exactly that reason. Adding the amplifier again at a new address updates the existing
entry's host and keeps entity history.

## Testing

The device is single-session, has no NAK and matches replies by position, so no test
touches it:

| Layer | Fake | Where |
|---|---|---|
| Protocol client | The real client over a patched `asyncio.open_connection` with scripted 50-byte replies | `test_protocol.py` |
| Power | The real coordinator over `FakeAmp`, which encodes the measured power behaviour and can be told to misbehave | `test_power.py` |
| Entities, HTTP, config flow | Mocked client, coordinator or HTTP session | `test_media_player.py`, `test_http_api.py`, `test_config_flow.py` |

Cases that exist because this device bites there: NUL-padded replies, **both** volume reply
forms (`Vol=-27db` echo, `Vol=-27 db` query), the dB↔byte round-trip over −70…+12, groups
that return nothing, reads that time out, and the frame builder refusing every forbidden
opcode.

## Distribution

A HACS custom repository. The protocol client is bundled in `protocol.py`, not published to
PyPI: right for one target, and revisitable if this heads for Home Assistant core, where
`dependency-transparency` requires a published library.

`manifest.json` claims no `quality_scale` tier until one is met;
[`quality_scale.yaml`](../custom_components/sonance_dsp/quality_scale.yaml) records the
target and the exemptions that already apply.

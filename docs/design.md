# Design

Why the integration is shaped the way it is. The protocol itself is in
[`protocol.md`](protocol.md); this document is about the decisions layered on top, and
which of them are forced by the hardware rather than chosen.

Most of what follows is forced. That is the point of having reverse-engineered the device
first: several obvious designs are wrong here, and wrong in ways that do not show up until
they are in production.

## Scope

Local control of a Sonance DSP amplifier: per-zone volume, mute, source and power, exposed
as `media_player` entities, with zones discovered from the device rather than configured by
hand.

**Non-goals**, deliberately:

- **Audio streaming.** The amplifier has no network audio input — only line inputs from two
  modular slots. It amplifies whatever is wired to it. It cannot be a Music Assistant
  player or a Squeezelite target, and no amount of integration work changes that.
- **DSP tuning.** Crossovers, EQ curves and speaker presets are configured in the
  amplifier's own web UI, by someone who knows what the speakers are. Exposing a 50-entry
  preset list to an automation engine invites a mistake with a physical consequence.
- **Channel-to-group assignment.** See [Forbidden operations](#forbidden-operations).

## Two transports

The amplifier answers on TCP 52000 and HTTP 80, and neither alone is sufficient.

| | TCP 52000 | HTTP 80 |
|---|---|---|
| Set volume / mute / source / power | ✅ | — |
| Read volume / mute / source | ✅ | partial |
| **Read per-group power** | ❌ *no such opcode* | ✅ |
| Serial, model, firmware | ❌ | ✅ |
| Zone and source **names** | partial | ✅ |
| Channel→group map | "empty or not" | ✅ authoritative |

So: **TCP for writes and fast state, HTTP for identity, discovery and group power.**

The HTTP JSON API is undocumented by the vendor — it was found by reading the web UI's own
JavaScript. That makes it a dependency worth naming honestly: it is not contractual, and a
firmware update could move it. The integration should degrade rather than fail if it
disappears, losing group power and device-supplied names but keeping volume control.

There is also an `action=write` form on the same endpoint. This integration does not use it.
Everything writable is writable over TCP, and the HTTP write path was observed accepting a
request it did not apply.

## Connection model

Three measured device behaviours, each of which rules out an otherwise reasonable design:

**One TCP session, held for the config entry's lifetime.** With two sockets open
concurrently, the *first* socket received both replies and the second received nothing. A
second connection does not merely fail — it silently corrupts the first socket's reply
stream, which is far worse, because the failure surfaces as wrong values rather than as an
error. This rules out a connection pool, and it rules out connect-per-command.

**Positional correlation, serialised by a lock.** Replies carry no sequence number and no
request id. The Nth reply belongs to the Nth command and nothing in the payload can prove
it — which is another reason a second connection is intolerable.

An earlier draft of this document specified pipelining: push a `Future` per command onto a
queue, let a reader task resolve the head. The device does pipeline correctly — N queries
down one socket return N ordered replies — so this works right up until a command gets no
reply. Then the queue is permanently one ahead of the stream, and *every subsequent reply
resolves the wrong request*, silently, reporting one zone's volume as another's.

That is not hypothetical here: a group with no channels answers with silence, so the
no-reply case is a normal part of discovery rather than an error path.

The implementation therefore holds an `asyncio.Lock` across each write-then-read. It costs
a round trip per command on a poll cycle of a handful of commands, and in exchange a
timeout is a local event instead of a corrupting one. When a read does time out unexpectedly
the connection is dropped and reopened, because reconnecting is the only way to be *certain*
of alignment again.

**Fixed-width reads, never `readline()`.** Replies are exactly 50 bytes, NUL-padded, with
no terminator. `readexactly(50)` under a timeout; a line-oriented reader waits forever for
a newline that never comes.

**The group letter is a correlator, and must be checked.** Scoped replies echo their
`Group:` letter. That single field is the only way to prove a reply belongs to the query
that asked for it, and checking it is what makes a late reply detectable instead of
silently authoritative. A group that answers *late* rather than not at all is otherwise
indistinguishable from the next group answering promptly — one slow zone makes zone A
disappear and invents a phantom zone C, for the life of the config entry.

The getters raise on a mismatch, so a desync becomes a failed poll and a clean reconnect
instead of wrong values. Discovery instead discards the stale reply and continues, because
there the enumeration is cross-checked against the authoritative HTTP channel map anyway,
and failing setup over a momentarily slow amplifier would be worse than under-reporting a
zone the cross-check then restores.

`readexactly` does not consume its buffer until it holds all 50 bytes, so a cancelled read
leaves everything already delivered queued for the next reader. Every exit path from a
request therefore tears the socket down — including **cancellation from outside**, which
`asyncio.timeout` does not convert into `TimeoutError` and which Home Assistant raises
routinely when it cancels a coordinator refresh on reload.

**Shutdown must never be re-entered as a connect.** `disconnect()` is lock-free by necessity,
since a request calls the teardown from inside the locked region and `asyncio.Lock` is not
reentrant. That means it can land while another task is suspended in `open_connection`, where
it sees no writer to close and returns having closed nothing. The connect therefore re-checks
the closed flag once the socket is up and discards it if so. Without that, the new socket is
published onto a client already closed for good and nothing ever closes it — which on a device
with one control session locks out every later setup until Home Assistant restarts.

The sibling Triad AMS integration, built on what appears to be the same OEM platform, has the
same hole in the same place and reaches it from the other direction: its shutdown path nulls the
stream first, so the in-flight read fails as a *network* error and its worker dutifully
reconnects a connection that was just told to stop.

Connection loss is handled by **lazy reconnection**, not a backoff loop inside the client.
A failed read tears the socket down; the next command reopens it. The retry cadence is
therefore the coordinator's poll interval, and Home Assistant's own coordinator backoff
covers repeated failure — a second backoff loop underneath it would only fight with it.

Entity availability follows the coordinator's last update rather than the socket's state.
Keying it on the socket would mark every zone unavailable for a whole interval over one
late reply, since a late reply is exactly what tears the socket down.

### A consequence worth stating plainly

Because the amplifier accepts one control session, **this integration and any other
controller are mutually exclusive.** A Savant, Control4, RTI or Crestron system holding the
port will break this integration, and vice versa. That is a property of the hardware, not a
limitation to engineer around, and it belongs in the troubleshooting docs.

## Polling, not push

`iot_class` is `local_polling`. Nothing in the vendor documentation mentions unsolicited
messages, and three independent third-party drivers all poll.

This is **untested rather than proven** — nobody has held a socket idle and changed the
volume from the front panel to see what arrives. If the amplifier does push, switching to
`local_push` is a small change and a large UX improvement. The coordinator is structured so
that switch does not require rewriting the entities.

## Entity model

**One device** — the amplifier, identified by its serial number — with **N entities**, not
a device per zone. The zones are groups inside a single box with a single serial, a single
firmware and a single network address; modelling them as separate devices would invent a
hierarchy the hardware does not have.

### Zone entities

One `media_player` per populated group. Names come from the device: the member channels'
names with the L/R suffix stripped, so `Patio L` and `Patio R` become **Patio**.

Zones are **discovered, not configured**. Query volume on groups `0x00`–`0x07`; an empty
reply means the group has no channels. Cross-check against the HTTP channel→group map,
because empty replies are also what a churning connection produces — the enumeration is
only trustworthy on a settled socket.

Groups can gain and lose channels through the web UI, so this is not a one-time fact.

### The amp-level entity

One additional `media_player` driving the protocol's global commands, which set every group
at once.

This exists for a specific reason. **Music Assistant maps exactly one entity per player to
a volume control**, but one source device commonly feeds several zones of the same
amplifier. Without a single entity representing "all of it", MA's volume control has
nothing coherent to point at. It is also the natural target for "turn the music down"
by voice.

Its state is the **mean of the populated groups' volumes**, because the device has no global
volume query. That average is a lie whenever zones have been trimmed apart, so the entity
surfaces an attribute saying so rather than quietly presenting a number nobody set.

### Feature flags, and why they are not cosmetic

```python
_attr_device_class = MediaPlayerDeviceClass.RECEIVER
_attr_supported_features = (
    MediaPlayerEntityFeature.VOLUME_SET
    | MediaPlayerEntityFeature.VOLUME_STEP
    | MediaPlayerEntityFeature.VOLUME_MUTE
    | MediaPlayerEntityFeature.TURN_ON
    | MediaPlayerEntityFeature.TURN_OFF
    | MediaPlayerEntityFeature.SELECT_SOURCE
)
```

`RECEIVER` and `VOLUME_STEP` are load-bearing, and the choice is a **trade, not a free
win**. HomeKit Bridge routes `media_player` by device class:

- `RECEIVER` routes to the receiver accessory — the only route that carries a real volume
  characteristic. It builds its speaker service **only if `VOLUME_MUTE` or `VOLUME_STEP` is
  present**, so `VOLUME_SET` alone yields nothing there.
- `SPEAKER`, or unset, falls through to feature validation. With `VOLUME_MUTE` declared the
  entity does bridge — as a mute-only switch accessory with **no volume at all**. It is
  dropped entirely only when `VOLUME_MUTE` is absent too.

The cost of `RECEIVER`: Home Assistant treats `TV`/`RECEIVER`/`PROJECTOR` as
**accessory-mode only**, and a HomeKit bridge created through the UI sets
`exclude_accessory_mode`, so these zones are **silently excluded from it** — no warning.
Exposing them to HomeKit means a separate HomeKit instance per zone, which is what every
AVR integration requires and is standard HA behaviour rather than a defect here.

So: `RECEIVER` buys a real volume slider at the price of per-zone pairing; `SPEAKER` buys
bridging at the price of having no volume, which is the wrong half of the trade for an
amplifier. Alexa and Assist are satisfied by `VOLUME_SET` alone and are unaffected either
way. None of this is visible until someone opens the Home app.

### Why not `number` entities for volume

A `number` would give long-term statistics, which `media_player` volume (an attribute)
does not. It would also lose the media control card, Assist's volume intents, Alexa's
Speaker interface and HomeKit entirely — `number` is not bridged. The trade is not close.

If volume history matters later, add a `number` at `EntityCategory.DIAGNOSTIC` *alongside*
the `media_player`, rather than moving the primary control.

## Known limitations

Recorded here rather than left to be rediscovered:

- **Zone names are read once, at setup.** Renaming a zone in the amplifier's web UI does
  not propagate until the config entry is reloaded, and neither does adding or removing a
  zone. The channel map is only read during discovery.
- **Group power is visible but not controllable.** It is surfaced as an attribute; there is
  no `TURN_ON`/`TURN_OFF` yet, so the entity deliberately never renders `OFF` — a zone
  shown as off with no way to turn it on is a dead tile.
- **Mute and source reply formats are unverified.** Only the volume and amplifier-power
  literals have been captured from real hardware. The parsers are case-insensitive and log
  a warning with the raw text when a reply arrives but does not match, so a format surprise
  becomes a bug report rather than a permanently dead control.
- **One controller at a time.** See the connection model: this integration and any
  Savant/Control4/RTI system are mutually exclusive.

## Volume

The device range is **−70 to +12 dB** in 1 dB steps, `byte = dB + 183`.

Home Assistant's 0.0–1.0 maps onto −70 dB up to a **per-zone configurable maximum,
defaulting to 0 dB**.

The +12 dB ceiling is deliberately not the default. It is the factory turn-on level, and
the vendor's own integrator notes single it out as something to change before commissioning.
A volume slider whose right-hand end is maximum gain is a slider that will eventually be
dragged there by a phone in a pocket. Raising the ceiling stays possible, but as a conscious
act in the options flow.

`_attr_volume_step` is derived from the configured range so `volume_up` / `volume_down`
move exactly one device step rather than Home Assistant's default 10%.

## State the device will not tell you

**Group power has no query opcode.** It can be set over TCP and read over HTTP, and that
asymmetry has to be handled rather than hidden: if the HTTP read is unavailable, group power
becomes optimistic state and will drift when someone uses the front panel or the web UI.

Where optimistic state is unavoidable, it should be visibly optimistic — refreshed on every
poll that can refresh it, and never presented as more certain than it is.

## Forbidden operations

**Opcodes `0x21`–`0x28` reassign channels between groups. The integration must never send
them.**

They are destructive, there is no safe inverse without a prior settings backup, and — the
part that makes them genuinely dangerous — **the echo lies.** An assignment returned
`Channel <name> group is B` for a change that was never applied; the authoritative HTTP
map still read the old value afterwards. The vendor's own HTTP write endpoint failed the
same way, also reporting success.

This is enforced in the frame builder against `FORBIDDEN_OPCODES`, not left to convention,
precisely because a mistake here is invisible at the point of the mistake.

The general lesson is worth carrying beyond these opcodes: **this device's echo is not
confirmation.** Read back through HTTP before believing a configuration change.

Group topology belongs in the amplifier's web UI.

## Error handling

Every write is wrapped so protocol errors surface as translated `HomeAssistantError`
rather than raw exceptions, naming the entity and the operation.

Connection loss logs once at `info` on the way down and once on the way back up — not on
every failed poll. A device that is off overnight should not produce hundreds of log lines.

Setup failure raises `ConfigEntryNotReady` so Home Assistant retries with backoff, rather
than returning `False` and requiring manual intervention.

## Identity

The config entry's `unique_id` is the amplifier's **serial number**, read over HTTP.

These amplifiers are commonly on DHCP, so the IP address is not identity — and Home
Assistant's own rules disallow IP, hostname and device name as unique-id sources for exactly
this reason. Keying on the serial means a DHCP move requires reconfiguring the host, not
rebuilding the entry and losing entity history.

## Testing

The device is awkward to test against: single-session, no NAK format, and a protocol whose
replies are positional. Three tiers:

1. **Fake client object** — most entity tests. Patch at a factory seam in `protocol.py`.
2. **Fake transport under the real client** — patch `asyncio.open_connection` with a reader
   pre-fed canned 50-byte frames. This is where framing, FIFO correlation and reconnect get
   tested.
3. **Loopback protocol emulator** — `asyncio.start_server` running a small state machine.
   Best value for reply-format edge cases.

Cases that exist because this specific device bites there:

- 50-byte NUL-padded reply, trailing NULs stripped
- **both** reply forms — `Vol=-27db` (command echo) and `Vol=-27 db` (query)
- dB↔byte round-trip across the full −70…+12 range
- zone enumeration where some groups return nothing
- a read that times out rather than returning 50 bytes
- the frame builder refusing every opcode in `FORBIDDEN_OPCODES`

## Distribution

A HACS custom repository. The protocol client is **bundled** in `protocol.py` rather than
published to PyPI — appropriate for a single-target integration, and revisitable if this
ever heads toward Home Assistant core, where `dependency-transparency` would require a
published library.

`manifest.json` carries no `quality_scale` key. Claiming a tier before meeting it would be
a false claim; [`quality_scale.yaml`](../custom_components/sonance_dsp/quality_scale.yaml)
records the target and the exemptions that already apply.

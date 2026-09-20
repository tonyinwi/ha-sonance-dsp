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

**FIFO future correlation.** A single socket pipelines correctly: N queries return N
ordered replies. Each command pushes a `Future` onto a queue; a reader task resolves the
head. Replies carry no sequence number and no request id, so ordering *is* the correlation
— which is another reason a second connection is intolerable.

**Fixed-width reads, never `readline()`.** Replies are exactly 50 bytes, NUL-padded, with
no terminator. `readexactly(50)` under a timeout; a line-oriented reader waits forever for
a newline that never comes.

Connection loss is handled by an exponential backoff capped at 30 seconds, with a
connection-state callback so entities flip to unavailable rather than serving stale values.

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

`RECEIVER` and `VOLUME_STEP` are load-bearing. HomeKit Bridge routes `media_player` by
device class:

- `SPEAKER`, or unset, produces an accessory class with **no volume characteristic at all** —
  and a volume-only entity is then dropped entirely as having "no supported features".
- `RECEIVER` routes to the receiver accessory, which builds its speaker service **only if
  `VOLUME_MUTE` or `VOLUME_STEP` is present.**

So `VOLUME_SET` alone — the obvious minimal choice for an amplifier — yields nothing in
HomeKit. Alexa and Assist are satisfied by `VOLUME_SET` on its own; HomeKit is the binding
constraint, and it is invisible until someone opens the Home app.

### Why not `number` entities for volume

A `number` would give long-term statistics, which `media_player` volume (an attribute)
does not. It would also lose the media control card, Assist's volume intents, Alexa's
Speaker interface and HomeKit entirely — `number` is not bridged. The trade is not close.

If volume history matters later, add a `number` at `EntityCategory.DIAGNOSTIC` *alongside*
the `media_player`, rather than moving the primary control.

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

# Sonance DSP for Home Assistant

Local control of Sonance DSP amplifiers over IP — per-zone volume, mute, source and power
as `media_player` entities.

> **Status: scaffolding.** The protocol is reverse-engineered, verified against real
> hardware and documented in [`docs/protocol.md`](docs/protocol.md). The integration itself
> is not implemented yet.

## Why

The amplifier has a TCP control protocol that nothing in Home Assistant speaks. The usual
workaround — `shell_command` with `nc` and regex `command_line` sensors — gives you no
entities, no state, no volume slider, and fails silently when the amp is unreachable.

It also fills a real gap in a common topology. A streamer feeding a Sonance amp is often
set to **fixed line-out**, which means the streamer's own volume control does nothing and
the amplifier is the only working volume in the room.

## Supported devices

| Model | Status |
|---|---|
| DSP 8-130 (MkII / MkIII) | developed against a MkII, firmware V2.2.8130 |
| DSP 2-150, DSP 2-750 | same protocol, 2 sources and group A only — untested |

Requires the amplifier to be reachable on TCP 52000 and HTTP 80.

## Design

Two transports, because each knows something the other does not:

- **TCP 52000** — binary control. Setting volume, mute, source, power.
- **HTTP 80** — an undocumented JSON API found in the web UI's own JavaScript. Supplies the
  serial number, model, firmware, zone and source names, and **per-group power status**,
  which the TCP protocol genuinely cannot report.

Three measured device behaviours shape the client, and all three break naive
implementations:

1. **The amplifier accepts exactly one TCP session.** A second concurrent connection
   receives nothing *and* its replies are delivered to the first socket, silently corrupting
   that stream. The integration holds one connection and never opens a second.
2. **Replies are exactly 50 bytes, NUL-padded, with no terminator.** A `readline()` client
   hangs forever.
3. **The query reply and the command echo use different whitespace** — `Vol=-27 db` versus
   `Vol=-27db`. A parser written against one silently fails on the other.

## Documentation

| | |
|---|---|
| [`docs/protocol.md`](docs/protocol.md) | The protocol, with the evidence — and every place the device contradicts the vendor documentation. |
| [`docs/design.md`](docs/design.md) | Why the integration is shaped this way, and which decisions are forced by the hardware rather than chosen. |
| [`docs/roadmap.md`](docs/roadmap.md) | What ships when, the MVP's exit criteria, and the open questions. |

## Volume

The device range is −70 to +12 dB. Home Assistant's 0–100% maps onto −70 dB up to a
**per-zone configurable maximum, defaulting to 0 dB**.

The +12 dB ceiling is reachable but deliberately not the default: it is the factory turn-on
level, and Sonance's own integrator notes flag it as a hazard. Raising it is a conscious act.

## Installation

Via HACS as a custom repository:

1. HACS → ⋮ → Custom repositories
2. Add `https://github.com/tonyinwi/ha-sonance-dsp`, category **Integration**
3. Install, restart Home Assistant
4. Settings → Devices & Services → Add Integration → **Sonance DSP**

Enter the amplifier's IP. The entry is keyed on the amplifier's **serial number**, so a
DHCP address change will not orphan it.

## ⛔ What this integration will not do

**It never sends the channel-to-group assignment opcodes (`0x21`–`0x28`).**

They are destructive, there is no safe inverse without a prior backup, and the device
*echoes success for changes it did not apply* — `Channel <name> group is B` came back for an
assignment that never happened, with the authoritative map still reading the old value
afterwards. Group topology belongs in the amplifier's own web UI at
`http://<amp>/BasicSetting.htm`.

**Take a settings backup before doing anything unusual** — GeneralSettings.htm → BACKUP
RESTORE → All Settings. The `.gen` file encodes the channel→group map, so it will restore a
clobbered topology.

## Music Assistant

The amplifier is not a Music Assistant player and cannot become one — it has no network
audio input, only line inputs. MA plays to whatever feeds the amp.

The seam is MA's **player controls**: map this integration's amp-level entity to the MA
player's **Volume** control. Where the source device is set to fixed output, this is the
only thing that makes MA's volume slider do anything.

Leave **mute native** if the source device's mute works — and in that case never use MA's
*FAKE* mute, which works by driving volume to zero, the one control that does nothing.

## Credits

Protocol reverse-engineered from Sonance's published IP command spreadsheet and Savant
profile, cross-checked against the openHAB 1.x Sonance binding, and then **verified against
live hardware** — which is where most of the corrections above came from.

## License

MIT

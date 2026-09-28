# Sonance DSP for Home Assistant

Local control of Sonance DSP amplifiers over IP: per-zone volume, mute, source and power
as `media_player` entities. Nothing else in Home Assistant speaks their TCP protocol, and
behind a streamer at **fixed line-out** the amp is the only working volume in the room.

> [!WARNING]
> **Personal project — provided as is, without support.**
>
> I built this for one installation: my own amplifier, network and Home Assistant. Its
> decisions are made for that system. It is published in case it is useful to someone, not
> as a supported product.
>
> - **No support.** Issues and pull requests may go unanswered. There is no commitment to
>   fix bugs, add features, support other models or firmware, or keep pace with Home
>   Assistant releases.
> - **No warranty.** Provided as is, under the [MIT license](LICENSE). It switches real
>   audio equipment on and off and sets its volume, so test it carefully on your own system
>   before relying on it.
> - **Breaking changes** can land in any release, without notice or a migration path.
> - **Not affiliated with Sonance.** Sonance is a trademark of its owner. This project is
>   not made, endorsed or supported by them.
>
> If it does not do what you need, fork it.

**Status.** Volume, mute, source and upstream mirroring have run on a live install since
0.2.0; zone power since 0.3.0, with a silent power-up since 0.3.1.

## Supported devices

| Model | Status |
|---|---|
| DSP 8-130 MKII | developed against it, firmware V2.2.8130 |
| DSP 8-130 MKIII | untested |
| DSP 2-150, DSP 2-750 | same protocol, 2 sources and group A only; untested |

Needs Home Assistant 2026.9 or later, and the amp reachable on TCP 52000 and HTTP 80. The
amp allows **one control connection**, which this holds, so it cannot share an amp with
another controller (Savant, Control4, RTI, Crestron).

## What works

Each zone (output group) is its own player, as in Control4 room audio; there is no
amp-level entity ([why](docs/design.md#the-zone-is-the-player-there-is-no-amp-level-entity)).
Zones are discovered and named from the amp, and each has:

- **Volume**, set absolutely; up/down move 1 dB.
- **Mute.**
- **Source**, by the input names set on the amp.
- **Mirroring**: link a source to the player feeding it, and zones on it show its track,
  artwork and play state.
- **Power**, owned by Home Assistant ([below](#power)).
- **Transport and media**: play, pause, stop, play media and browse go to the linked
  player, while the zone is on. Playback belongs to the source, so every zone on it
  follows: in a scene meant to control playback, include the player, not the zones. No
  skip, no search, and no announcements or text-to-speech: a zone drops them with a
  warning, so send them to the player.

Not yet: `media_player.join`, diagnostics.

**HomeKit:** zones are receivers, the only class HomeKit gives volume control, so each
zone needs its own accessory-mode HomeKit instance. A HomeKit Bridge set up with the Media
player domain ticked creates one for every TV and receiver in the house, not only the
zones. To add one zone, set up a bridge without that domain, then set its mode to
Accessory and pick the zone.

## Volume

Range −70 to +12 dB. The slider tops out at a ceiling set in the options for all zones,
**0 dB by default**, or at the amp's own per-channel maximum if lower. Going above 0 dB is
opt-in ([why](docs/design.md#volume)).

> [!CAUTION]
> For about a second after a zone is switched on, the amp plays it **unmuted at its
> turn-on volume** (per zone, In/Out Settings tab; factory value +12 dB), whatever its mute
> or the ceiling. **Set every zone's turn-on volume to −70 dB.** The integration then puts
> the zone back at its previous level once that second has passed.

## Power

Home Assistant owns power, built around one failure: music starting where nobody wants
it. That needs the amp's **Auto On method at Power Button, with every channel's sleep
off**, so nothing wakes by itself. The integration checks these, and every zone's turn-on
volume, at setup and daily, and raises a repair issue for any that is wrong.

- Switching a zone on restores its mute and then its volume, and checks both.
- A muted zone is never sent a volume: on this amp any volume change un-mutes. Its level
  is held and applied when it is unmuted.
- An off zone's volume can't be changed: the amp would keep it as the zone's next
  switch-on level.
- Waking the amp takes about 10 s, and brings back only the zone asked for.
- A zone already on is left alone.
- Switching the last zone off puts the amp in standby.
- When something cannot be read, nothing is guessed.

The rules, and what each defends against: [`docs/design.md`](docs/design.md#power).

## Installation

Via HACS, as a custom repository:

1. HACS → ⋮ → Custom repositories
2. Add `https://github.com/tonyinwi/ha-sonance-dsp`, category **Integration**
3. Install, then restart Home Assistant
4. Settings → Devices & Services → Add Integration → **Sonance DSP**, and enter the amp's IP

The entry is keyed on the amp's **serial number**, not its IP: after an address change, add
it again at the new address to update it. Options: volume ceiling, polling interval (default
10 s), and the media player feeding each source.

## What this integration will not do

**It never sends the channel-to-group assignment opcodes `0x21`–`0x28`**, and the frame
builder refuses them. They are destructive, cannot be undone safely without a backup, and
the amp *echoes success for changes it did not apply*. Set group topology in the amp's web
UI at `http://<amp>/BasicSetting.htm`. Evidence:
[`docs/design.md`](docs/design.md#forbidden-operations).

**Back up the settings before anything unusual**: GeneralSettings.htm → BACKUP RESTORE →
All Settings. The `.gen` file encodes the channel→group map, so it restores a clobbered
topology.

## Music Assistant

The amp cannot be a Music Assistant player: it has only line inputs, so MA plays to
whatever feeds it. In MA's player controls, map a zone to that player's **Volume** control;
at fixed output, that is the only way MA's volume slider does anything. A source feeding
several zones has no single volume to map: by design, each zone keeps its own.

Leave **mute native** if the source's mute works, and then never use MA's *FAKE* mute: it
drives volume to zero, the one control that does nothing.

Don't add zones as players in MA's Home Assistant provider: a zone only forwards to the
player feeding it.

## More

[`docs/protocol.md`](docs/protocol.md) has the device facts and what was and was not measured,
[`docs/design.md`](docs/design.md) the decisions and known limitations, and
[`docs/roadmap.md`](docs/roadmap.md) what is next.

The protocol comes from Sonance's IP command spreadsheet and Savant profile and the openHAB
1.x Sonance binding, corrected against live hardware.

# Roadmap

Narrow first, then branching. The transport is the risky part, so it was proven under one
entity before anything else depended on it.

Status: **0.5.1.** Device facts are in [`protocol.md`](protocol.md), decisions in
[`design.md`](design.md).

## Done

| Stage | Shipped |
|---|---|
| MVP | Protocol client, refusing the forbidden opcodes at the frame builder ([why](design.md#forbidden-operations)), HTTP identity, config flow keyed on serial (with `cannot_connect` recovery and duplicate-serial abort), coordinator, per-zone volume and mute |
| 2 — all zones | Populated groups enumerated over TCP, cross-checked against the HTTP channel map, named from the device |
| 2a — upstream mirroring | A zone shows title, artist, album, artwork and transport state from the `media_player` linked (per source, in the options flow) to its current source |
| 3 — source and power | `SELECT_SOURCE` by the device's input names. Zone power, owned by Home Assistant: see [Power](design.md#power) |
| Transport | Play, pause and stop passed to the linked player, only while the zone is on |
| Media | Play media and browse through the linked player, only while the zone is on; no search or announcements |

The amp-level entity planned for stage 2 was dropped by design: see
[the zone is the player](design.md#the-zone-is-the-player-there-is-no-amp-level-entity).
Music Assistant needs nothing more: each zone maps as an MA player's volume control
([README](../README.md#music-assistant)).

**MVP live checks.** Tests cannot make these. Done 2026-09-28 on zone D, with the source
idle and each level read back from the amplifier's own page (`output-volumes`,
`mute-volumes`):

- The slider moves the amplifier and the read-back matches: −30 → −40, up to −39, down to
  −40.
- A volume set while muted is held: HA showed −35, the amplifier stayed muted at −40
  through two polls, and unmuting applied −35.
- On 0.5.0, with zones A–C muted: browsing zone D showed Music Assistant's library; play
  media from zone D started the Sonos once (one idle → playing transition), audible on D
  only; −40 → −35 was louder; pause from zone D stopped it. A playlist that Music Assistant
  held empty (its provider needed signing in again) failed with MA's own error, shown in
  HA.
- A volume change outside HA appears within one poll: zone D set to −45 through the
  amplifier's own web endpoint (`in-out-settings`, `name=output-volume`) showed in HA 3.5 s
  later, and so did the change back to −38 (10 s poll).
- A pulled network cable: the amplifier went at 13:58:23, all four zones were unavailable
  at 13:58:27, and they recovered on their own at 14:00:27, no restart, state intact
  (0.5.1). One error logged going down, one line coming back.
- Assist: "set Back Yard Output 4 volume to 30 percent" on zone D, on and playing, took it
  from −40 to −49 dB (30 % of −70…0), read back from the amplifier.

Not yet done:

- HomeKit, deferred: a zone in its own accessory-mode instance, with volume from the iOS
  Remote. The only check that catches a wrong `device_class` or a missing `VOLUME_STEP`.
- Assist on a zone that is off: the refusal (0.5.1).

## Next

Transport and media are done. In order:

### 4 — Diagnostics and configuration

- Short-protect and over-temperature per channel (`0x17` / `0x18`) as binary sensors:
  fault reporting the amplifier already computes and nothing surfaces. The opcodes are
  defined; nothing queries them yet.
- `diagnostics.py` is a scaffold. Implement it, redacting the keys already in `TO_REDACT`:
  the network block, serial and installer/customer/dealer names.
- DSP preset as a `select` at `EntityCategory.CONFIG`, **read-only or omitted**: see the
  DSP-tuning non-goal in [Scope](design.md#scope).

### 5 — Quality scale

Bronze, then Silver, tracked in
[`quality_scale.yaml`](../custom_components/sonance_dsp/quality_scale.yaml).
`quality_scale` goes into `manifest.json` only once a tier is met.

### Grouping, once there is a second source

Route zones onto a common source with `media_player.join`, which follows from
[the zone is the player](design.md#the-zone-is-the-player-there-is-no-amp-level-entity).
With one streamer feeding every zone, all zones already share its source and a join would
do nothing, so this waits for a second streamer on input 2 or 4.

### 6 — Other models

The DSP 2-150 and 2-750 differ in two parameters ([protocol](protocol.md#other-models)).
Discovery already finds whatever groups exist; `SOURCE_COUNT` is a fixed 4 and needs to
become per-model. Confirming needs the hardware.

### Power follow-ups: closed

*2026-09-28:* zone-on to a zone already on was measured (it does
nothing, [protocol](protocol.md#power-measured-in-power-button-mode)); what is still
unmeasured, power-on to an amp already on and standby to one in standby, nothing depends on.

## Open questions

Ranked by how much they would change the design.

1. **What does a malformed frame or out-of-range volume byte return?** No NAK format is
   documented, so an unrecognised reply is logged and treated as no answer.
2. **Does the amplifier drop an idle socket?** Decides whether a keepalive is needed.
3. **Does it advertise over mDNS, or have a stable DHCP fingerprint?** Would enable
   discovery, a Gold requirement.
4. **Does audio sense push a frame?** Untested; it would not change polling.

Settled, with evidence in [`protocol.md`](protocol.md): the amplifier does not push state
(2026-09-20), and the status page shows a TCP zone-power change in 0.01–0.06 s
(2026-09-26).

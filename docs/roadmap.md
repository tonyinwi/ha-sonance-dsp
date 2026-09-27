# Roadmap

Narrow first, then branching. The transport is the risky part, so it was proven under one
entity before anything else depended on it.

Status: **0.4.0.** Device facts are in [`protocol.md`](protocol.md), decisions in
[`design.md`](design.md).

## Done

| Stage | Shipped |
|---|---|
| MVP | Protocol client, refusing the forbidden opcodes at the frame builder ([why](design.md#forbidden-operations)), HTTP identity, config flow keyed on serial (with `cannot_connect` recovery and duplicate-serial abort), coordinator, per-zone volume and mute |
| 2 — all zones | Populated groups enumerated over TCP, cross-checked against the HTTP channel map, named from the device |
| 2a — upstream mirroring | A zone shows title, artist, album, artwork and transport state from the `media_player` linked (per source, in the options flow) to its current source |
| 3 — source and power | `SELECT_SOURCE` by the device's input names. Zone power, owned by Home Assistant: see [Power](design.md#power) |
| Transport | Play, pause, stop and skip passed to the linked player, only while the zone is on |

The amp-level entity planned for stage 2 was dropped by design: see
[the zone is the player](design.md#the-zone-is-the-player-there-is-no-amp-level-entity).
Music Assistant needs nothing more: each zone maps as an MA player's volume control
([README](../README.md#music-assistant)).

**MVP live checks.** Tests cannot make these, and nothing in the repo records them as done:

- The slider moves the amplifier and the read-back matches.
- A volume change in the amplifier's web UI appears in HA within one poll interval.
- A pulled network cable marks entities unavailable within one interval, and they recover
  without restarting Home Assistant.
- The zone appears in HomeKit **with a working volume slider**: the only check that
  catches a wrong `device_class` or a missing `VOLUME_STEP`.
- Assist: "set \<zone\> volume to 30 percent".

## Next

### Transport and grouping

Transport is done. Next: play and browse media through the linked player, then route
zones onto a common source with `media_player.join`. Both follow from
[the zone is the player](design.md#the-zone-is-the-player-there-is-no-amp-level-entity).

### Power follow-ups

- **Measure what the code defends against blind**, listed under *not measured* in
  [Power: measured in Power Button mode](protocol.md#power-measured-in-power-button-mode).
  Each needs the amplifier and a go-ahead, with the source idle. Zone-on to a zone already
  on matters most: it would settle the one power rule that is a choice.

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

### 6 — Other models

The DSP 2-150 and 2-750 differ in two parameters ([protocol](protocol.md#other-models)).
Discovery already finds whatever groups exist; `SOURCE_COUNT` is a fixed 4 and needs to
become per-model. Confirming needs the hardware.

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

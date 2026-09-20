# Roadmap

Deliberately narrow first, then branching. The MVP is one thing working correctly rather
than everything working approximately, because the transport is the risky part and it is
cheaper to find out it is wrong while there is one entity depending on it.

Status: **scaffolding.** The protocol is verified against hardware
([`protocol.md`](protocol.md)) and the design settled ([`design.md`](design.md)). No
implementation yet.

---

## MVP — volume on one zone

Done when moving the volume slider in Home Assistant moves the amplifier, and the amplifier
being moved elsewhere shows up in Home Assistant.

1. **`protocol.py`** — connect, send one frame, read exactly 50 bytes, parse. Tolerate both
   reply whitespace forms. Refuse `FORBIDDEN_OPCODES` at the frame builder.
2. **`http_api.py`** — read `general-settings` for serial, model, firmware, name.
3. **`config_flow.py`** — host → HTTP identity read → `unique_id` = serial → entry, titled
   from the amplifier's own name. Cover `cannot_connect` **and recovery from it**, plus the
   duplicate-serial abort.
4. **`coordinator.py`** — poll volume and mute for one group.
5. **`media_player.py`** — one entity. `async_set_volume_level`, `volume_up` / `volume_down`,
   `mute_volume`.
6. **Verify end-to-end** against real hardware, not just tests.

**Exit criteria**, all of which are things tests alone will not tell you:

- The slider moves the amplifier and the read-back matches.
- Changing volume in the amplifier's web UI appears in HA within one poll interval.
- Pulling the amplifier's network cable marks entities unavailable within one interval, and
  they recover without restarting Home Assistant.
- The zone appears in HomeKit **with a working volume slider** — the check that catches a
  wrong `device_class` or a missing `VOLUME_STEP`, and the one that cannot be caught any
  other way.
- Assist: "set \<zone\> volume to 30 percent".

---

## 2 — All zones, plus the amp-level entity

Enumerate populated groups over a settled connection, cross-checked against the HTTP
channel→group map. One entity per zone, named from the device. Handle empty groups without
creating phantom entities, and handle a group that exists but has no speakers wired to it.

Add the amp-level entity on the global commands, with the drift attribute described in
[`design.md`](design.md#the-amp-level-entity).

## 2a — Music Assistant

Not an extra. Where the source device feeding the amplifier is set to **fixed output**, its
own volume control does nothing, and MA's volume slider for that player controls nothing
until it is mapped to this integration.

- Expose the amp-level entity to MA via the HA Plugin, set it as the player's **Volume**
  control.
- **Leave mute native** if the source device's mute works — fixed output bypasses the
  attenuator but not the gate, so mute usually still functions where volume does not.
- **Never use MA's FAKE mute** in that configuration: FAKE mute works by driving volume to
  zero, which is precisely the control that does nothing.
- Power mapping is optional. Amplifiers set to audio-sense auto-on wake themselves.

## 3 — Source and power

`SELECT_SOURCE` with names read from the device. Group power written over TCP and read back
over HTTP, since no group-power query exists.

Use the vendor's power-on sequence — amp on → group on (200 ms) → query volume (1000 ms) —
rather than a bare group-on. Their power-off sends group-off **twice**, which suggests one
proved unreliable; worth replicating rather than tidying away.

## 4 — Diagnostics and configuration

Short-protect and over-temperature per channel (`0x17` / `0x18`) as binary sensors — genuine
fault reporting the amplifier already computes and nothing currently surfaces.

`diagnostics.py`, redacting the network block and the installer/customer/dealer names.

DSP preset as a `select` at `EntityCategory.CONFIG`, **read-only or omitted** unless there
is a good argument otherwise. See the DSP-tuning non-goal in [`design.md`](design.md#scope).

## 5 — Quality scale

Bronze, then Silver, tracked in
[`quality_scale.yaml`](../custom_components/sonance_dsp/quality_scale.yaml). Add
`quality_scale` to `manifest.json` only once a tier is actually met.

## 6 — Other models

The DSP 2-150 and 2-750 speak the same protocol with **2 sources instead of 4** and **group
A only**. If the client carries those as parameters from the start, support is mostly a
matter of finding someone with the hardware to confirm.

---

## Open questions

Ranked by how much they would change the design.

1. **Does the amplifier push unsolicited state?** Would move this to `local_push`. Test by
   holding a socket idle and changing volume at the front panel.
2. **What does a malformed frame or out-of-range volume byte return?** No NAK format is
   documented anywhere, so error handling is currently "unparseable means failure".
3. **Is a TCP group-power change reflected in the HTTP status page, and how fast?** Decides
   whether group power needs optimistic state between polls.
4. **Does the amplifier drop an idle socket?** Decides whether a keepalive is needed.
5. **Does it advertise over mDNS, or have a stable DHCP fingerprint?** Would enable
   discovery, a Gold requirement.

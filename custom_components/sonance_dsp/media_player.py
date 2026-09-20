"""Media player platform for the Sonance DSP integration.

SCAFFOLD ONLY -- not implemented yet.

Two kinds of entity, both media_player:

* **One per populated group** -- the zones. Named from the device: member
  channels' ``output-names`` with the L/R suffix stripped, so ``Back Porch L``
  and ``Back Porch R`` become **Back Porch**.
* **One amp-level entity** driving the global commands (LEN_GLOBAL, no operand).
  Required, not cosmetic: Music Assistant maps exactly one entity per player to
  a volume control, and the single Sonos Port feeding this amp serves several
  zones. Without it MA's volume slider controls nothing. Its state is the mean
  of the populated groups' volumes -- the device has no global volume query --
  and it should surface an attribute when zones have drifted apart, so an
  average never silently misleads someone who has trimmed one zone.

Required feature set::

    _attr_device_class = MediaPlayerDeviceClass.RECEIVER
    _attr_supported_features = (
        MediaPlayerEntityFeature.VOLUME_SET
        | MediaPlayerEntityFeature.VOLUME_STEP    # HomeKit needs this
        | MediaPlayerEntityFeature.VOLUME_MUTE    # ...or this
        | MediaPlayerEntityFeature.TURN_ON
        | MediaPlayerEntityFeature.TURN_OFF
        | MediaPlayerEntityFeature.SELECT_SOURCE
    )
    _attr_volume_step = 1 / (max_db - MIN_VOLUME_DB)

``RECEIVER`` and ``VOLUME_STEP`` are load-bearing, not decoration. HomeKit Bridge
routes media_player by device class: ``SPEAKER`` or unset yields an accessory
with no volume characteristic at all, and a volume-only entity is then dropped
entirely as having "no supported features". ``RECEIVER`` routes to the receiver
accessory -- which builds its speaker service only if ``VOLUME_MUTE`` or
``VOLUME_STEP`` is present. ``VOLUME_SET`` alone gives you nothing there.
Alexa and Assist are happy with ``VOLUME_SET`` alone; HomeKit is the constraint.

Volume mapping: HA 0.0-1.0 spans ``MIN_VOLUME_DB`` to the per-zone ``max_db``
option, default 0 dB. The device reaches +12 dB but that is the factory turn-on
default the vendor flags as a hazard, so it is opt-in.

Group power is write-only over TCP -- read it back from the HTTP status page.

``PARALLEL_UPDATES = 1``: one socket, one command at a time outbound.
"""

from __future__ import annotations

PARALLEL_UPDATES = 1

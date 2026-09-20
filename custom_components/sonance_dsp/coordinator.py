"""Data update coordinator for the Sonance DSP integration.

SCAFFOLD ONLY -- not implemented yet.

Polls, because nothing suggests the amplifier pushes: three independent
third-party drivers all poll, and neither vendor artifact mentions unsolicited
messages. This is untested rather than proven -- if a held-open idle socket
turns out to receive front-panel changes, switch to ``local_push`` and drop the
interval. Until then ``iot_class`` stays ``local_polling``.

Each cycle, over the single persistent socket:

* volume, mute and source per populated group (TCP)
* per-group power (HTTP ``status`` -- there is no TCP opcode for it)

Zone enumeration happens once at setup: query volume on groups 0x00-0x07 and
cross-check ``output-groups`` from HTTP. An empty TCP reply means the group has
no channels -- but only trust that on a settled connection, since connection
churn also produces empty replies.

Pass ``config_entry`` explicitly. It is silently ignored for custom integrations
today, but it is what registers ``async_shutdown`` on unload.
"""

from __future__ import annotations

DEFAULT_SCAN_INTERVAL = 10

"""Diagnostics support for the Sonance DSP integration.

SCAFFOLD ONLY -- not implemented yet.

Dump the coordinator snapshot plus the three HTTP JSON pages, since together
they are the whole picture of the device's state.

**Redact the network block** from ``general-settings`` -- ``ip-address``,
``ip-subnet-mask``, ``gateway``, ``dns-server`` -- and consider whether
``serial-number``, ``customer-name``, ``dealer-name`` and ``installer-name``
belong in a file a user may paste into a public issue. They are personal
details, not device telemetry.
"""

from __future__ import annotations

TO_REDACT = {
    "ip-address",
    "ip-subnet-mask",
    "gateway",
    "dns-server",
    "serial-number",
    "customer-name",
    "dealer-name",
    "installer-name",
}

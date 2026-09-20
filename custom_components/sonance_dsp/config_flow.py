"""Config flow for the Sonance DSP integration.

SCAFFOLD ONLY -- not implemented yet.

User step: host (and port, defaulted) -> connect and read
``general-settings`` over HTTP -> ``unique_id = serial-number`` ->
``_abort_if_unique_id_configured(updates=user_input)``.

**Key on the serial, never the IP.** The amplifier is typically on DHCP; an
address change must not orphan the entry. Serial is also the only acceptable
identity source under HA's own rules -- IP, hostname and device name are not.

Title the entry from the device's own ``amplifier-name``, not the model.

Options flow uses ``OptionsFlowWithReload``. Do **not** pair
``entry.add_update_listener`` with a reloading config-flow method -- that
combination is deprecated as of 2026.6 and becomes an error in 2026.12.

Options: per-zone ``max_db`` (default 0) and the polling interval.

Reauth is exempt -- the device has no authentication.

Test coverage must include the ``cannot_connect`` path **and recovery from it**
in the same test, plus the duplicate-serial abort.
"""

from __future__ import annotations

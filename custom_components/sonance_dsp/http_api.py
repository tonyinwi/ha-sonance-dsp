"""Read-only client for the amplifier's HTTP JSON API.

SCAFFOLD ONLY -- not implemented yet.

This endpoint is undocumented by Sonance; it was found by reading the web UI's
own JavaScript. It is read-only *by our choice* -- an ``action=write`` form
exists and this integration must never use it.

It supplies four things the TCP protocol cannot:

* ``general-settings`` -> ``serial-number`` (the config entry ``unique_id``),
  ``amplifier-name``, ``amplifier-model``, ``firmware-version`` for DeviceInfo.
* ``status`` -> **per-group power and mute**. The TCP protocol has no
  group-power query at all, so this is the only source.
* ``basicsettings`` -> ``output-names`` and ``input-names``, so zone and source
  names are read from the device instead of typed into a config flow.
* ``basicsettings`` -> ``output-groups``, the authoritative channel-to-group map.
  TCP can only report "this group is empty or it is not".

Use HA's shared aiohttp session (``async_get_clientsession``) rather than
creating one. Send ``Accept-Encoding: gzip`` -- the amp serves compressed and a
naive fetch returns binary soup.
"""

from __future__ import annotations

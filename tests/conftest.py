"""Test fixtures for the Sonance DSP integration.

SCAFFOLD ONLY -- not implemented yet.

Harness: pytest-homeassistant-custom-component, ``asyncio_mode = auto``.

Fixtures this needs:

* ``enable_custom_integrations`` -- required for HA to load a custom component
  at all. Order matters: ``recorder_mock`` must come before it if ever used.
* An autouse fixture patching
  ``homeassistant.requirements.async_process_requirements`` so HA does not try
  to pip-install during every test.
* ``mock_setup_entry`` patching ``custom_components.sonance_dsp.async_setup_entry``
  for config-flow tests.
* A fake amplifier object patched at the factory boundary, mirroring the client's
  API surface and recording calls. Having a one-function seam in ``protocol.py``
  is what makes this cheap.

Three tiers of device mock, pick per test:

1. **Fake client object** -- fastest, covers entity behaviour. Most tests.
2. **Fake transport under the real client** -- patch ``asyncio.open_connection``
   to return a ``StreamReader`` pre-fed with canned 50-byte frames. This is how
   you test framing, FIFO correlation and reconnect.
3. **Real loopback server** -- ``asyncio.start_server`` running a tiny protocol
   emulator. Best value for the reply-format edge cases. Note pytest-socket is
   in HA's test requirements and blocks network by default; allow loopback.

Cases that must be covered because they are where this device bites:

* 50-byte NUL-padded reply parsed correctly, trailing NULs stripped
* the command-echo form (``Vol=-27db``) *and* the query form (``Vol=-27 db``)
* dB<->byte round trip across the full -70..+12 range
* zone enumeration where some groups return nothing
* a read that times out rather than returning 50 bytes
* the frame builder refusing every opcode in FORBIDDEN_OPCODES
"""

from __future__ import annotations

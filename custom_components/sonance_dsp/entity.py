"""Base entity for the Sonance DSP integration.

SCAFFOLD ONLY -- not implemented yet.

Provides the shared ``CoordinatorEntity`` base, ``DeviceInfo`` keyed on the
amplifier's serial number, and a ``@command`` decorator wrapping every write so
protocol errors surface as translated ``HomeAssistantError`` rather than raw
exceptions::

    def command(func):
        @wraps(func)
        async def wrapper(self, *args, **kwargs):
            try:
                await func(self, *args, **kwargs)
            except SonanceError as exc:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="command_failed",
                    translation_placeholders={
                        "function_name": func.__name__,
                        "entity_id": self.entity_id,
                    },
                ) from exc
        return wrapper

``_attr_has_entity_name = True`` throughout; ``_attr_name = None`` on the entity
that *is* its device so it inherits the device name verbatim.

One device (the amplifier), N entities (the zones plus the amp-level one) -- not
a device per zone. The zones are groups inside one box, and the HTTP API reports
a single serial for the whole unit.
"""

from __future__ import annotations

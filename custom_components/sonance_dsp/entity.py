"""Base entity for the Sonance DSP integration."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Concatenate

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SonanceCoordinator
from .protocol import SonanceError

MANUFACTURER = "Sonance"


def command[EntityT: "SonanceEntity", **P](
    func: Callable[Concatenate[EntityT, P], Awaitable[None]],
) -> Callable[Concatenate[EntityT, P], Awaitable[None]]:
    """Wrap a write so protocol errors surface as translated HA errors.

    Without this a dropped connection mid-service-call raises a raw socket
    error into the caller's automation trace, naming neither the entity nor
    the operation.
    """

    @wraps(func)
    async def wrapper(self: EntityT, *args: P.args, **kwargs: P.kwargs) -> None:
        try:
            await func(self, *args, **kwargs)
        except SonanceError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={
                    "function_name": func.__name__,
                    "entity_id": self.entity_id,
                },
            ) from err

    return wrapper


class SonanceEntity(CoordinatorEntity[SonanceCoordinator]):
    """Shared base: one device, many entities.

    The zones are groups inside a single box with one serial, one firmware and
    one address. Modelling each as its own device would invent a hierarchy the
    hardware does not have.
    """

    _attr_has_entity_name = True

    def __init__(self, coordinator: SonanceCoordinator) -> None:
        super().__init__(coordinator)
        identity = coordinator.identity
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, identity.serial)},
            manufacturer=MANUFACTURER,
            model=identity.model,
            name=identity.name,
            serial_number=identity.serial,
            sw_version=identity.firmware or None,
        )

    # Availability deliberately follows the coordinator's last update, not
    # client.is_connected. The socket is torn down and lazily reopened whenever
    # a single reply is late, so keying on it would mark every zone unavailable
    # for a whole interval over one dropped reply on the last query of a poll.
    # A poll that failed outright already sets last_update_success False.

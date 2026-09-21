"""Data update coordinator for the Sonance DSP integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import (
    CONF_SCAN_INTERVAL,
    DEFAULT_HTTP_PORT,
    DEFAULT_SCAN_INTERVAL,
    GROUP_LETTERS,
    MAX_GROUPS,
)
from .http_api import AmplifierIdentity, SonanceHttpApi, SonanceHttpError, Topology
from .protocol import GroupState, SonanceConnectionError, SonanceProtocol

_LOGGER = logging.getLogger(__name__)

type SonanceConfigEntry = ConfigEntry[SonanceCoordinator]


@dataclass
class SonanceData:
    """One poll's worth of amplifier state."""

    identity: AmplifierIdentity
    topology: Topology | None
    groups: dict[int, GroupState] = field(default_factory=dict)
    group_power: dict[int, bool] = field(default_factory=dict)
    amp_power: bool | None = None


class SonanceCoordinator(DataUpdateCoordinator[SonanceData]):
    """Polls the amplifier over TCP, with HTTP filling what TCP cannot answer.

    Polling rather than push: nothing in the vendor documentation mentions
    unsolicited messages and three independent third-party drivers all poll.
    That is suggestive, not proven -- nobody has held a socket idle and moved
    the front-panel control to see what arrives. If it turns out to push, the
    entities do not need to change.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: SonanceConfigEntry,
        client: SonanceProtocol,
        identity: AmplifierIdentity,
        host: str,
    ) -> None:
        interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=identity.name,
            update_interval=timedelta(seconds=interval),
        )
        self.client = client
        self.identity = identity
        self.http = SonanceHttpApi(
            async_get_clientsession(hass), host, DEFAULT_HTTP_PORT
        )
        self.groups: list[int] = []
        self._topology: Topology | None = None
        self._http_available = True

    async def async_discover(self) -> None:
        """Enumerate populated groups and read the channel layout.

        Runs once at setup, after the connection has settled: connection churn
        produces the same empty replies as an absent group, so discovering on a
        fresh socket can miss zones that exist.

        The TCP enumeration is cross-checked against the HTTP channel-to-group
        map, which is authoritative. They can only disagree if a reply arrived
        late and shifted the stream -- the protocol layer's group-letter check
        catches most of that, but silence from a slow group is invisible to it.
        On disagreement the HTTP answer wins, because it is a direct statement
        of the topology rather than an inference from who answered.
        """
        try:
            self._topology = await self.http.topology()
        except SonanceHttpError as err:
            # Not fatal. Without it we lose device-supplied zone names and the
            # cross-check, but the TCP enumeration still finds the zones.
            _LOGGER.warning(
                "Could not read channel layout over HTTP (%s); falling back to "
                "generic zone names and unverified enumeration",
                err,
            )
            self._http_available = False

        tcp_groups = await self.client.discover_groups()

        if self._topology is not None and self._topology.output_groups:
            http_groups = [
                g for g in range(MAX_GROUPS) if self._topology.group_members(g)
            ]
            if set(http_groups) != set(tcp_groups):
                _LOGGER.warning(
                    "Group enumeration disagrees: the amplifier answered on %s "
                    "but its channel map says %s. Using the channel map",
                    [GROUP_LETTERS[g] for g in tcp_groups],
                    [GROUP_LETTERS[g] for g in http_groups],
                )
            self.groups = http_groups
        else:
            self.groups = tcp_groups

        _LOGGER.debug(
            "Discovered groups: %s", [GROUP_LETTERS[g] for g in self.groups]
        )

    def apply_optimistic(self, group: int, **fields: object) -> None:
        """Update one group's cached state and push it to entities now.

        Without this the UI waits a whole poll interval after a write, and the
        coordinator's refresh debounce makes a dragged slider visibly snap back
        to its old value before settling on the new one.
        """
        if self.data is None:
            return
        current = self.data.groups.get(group)
        if current is None:
            return
        self.data.groups[group] = replace(current, **fields)
        self.async_set_updated_data(self.data)

    def group_name(self, group: int) -> str | None:
        return self._topology.group_name(group) if self._topology else None

    async def _async_update_data(self) -> SonanceData:
        try:
            groups = {g: await self.client.read_group(g) for g in self.groups}
            amp_power = await self.client.get_amp_power()
        except SonanceConnectionError as err:
            raise UpdateFailed(f"Amplifier unreachable: {err}") from err

        group_power: dict[int, bool] = {}
        try:
            group_power = await self.http.group_power()
            if not self._http_available:
                _LOGGER.info("HTTP status page is reachable again")
                self._http_available = True
        except SonanceHttpError as err:
            # Deliberately not an UpdateFailed. Losing the HTTP page costs
            # group power, not volume control, and taking every zone entity
            # unavailable over it would be a worse outcome than a missing
            # attribute.
            if self._http_available:
                _LOGGER.info("HTTP status page unavailable (%s)", err)
                self._http_available = False

        return SonanceData(
            identity=self.identity,
            topology=self._topology,
            groups=groups,
            group_power=group_power,
            amp_power=amp_power,
        )

    async def async_close(self) -> None:
        """Release the amplifier's single control session."""
        await self.client.disconnect()

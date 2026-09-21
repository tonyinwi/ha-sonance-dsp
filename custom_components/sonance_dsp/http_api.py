"""Read-only client for the amplifier's HTTP JSON API.

This endpoint is undocumented by Sonance -- it was found by reading the web
UI's own JavaScript. That makes it a dependency worth naming honestly: it is
not contractual and a firmware update could move it, so every caller here must
tolerate it being unavailable. Losing it costs group power and device-supplied
names; it must not cost volume control.

It is read-only *by our choice*. An ``action=write`` form exists on the same
endpoint and this integration never uses it: everything writable is writable
over TCP, and TCP is where the reply can be correlated to the request.

The reason is narrower than it once read here. A ``name=output-group`` write
was seen returning unchanged JSON for a change it did not apply, with the
amplifier in standby -- but a ``name=output-volume`` write applies reliably.
One field misbehaves, not the endpoint. Read over HTTP, write over TCP, rather
than maintaining a model of which fields can be trusted.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import aiohttp

from .const import (
    GROUP_LETTERS,
    HTTP_HANDLER_PATH,
    HTTP_PAGE_BASIC,
    HTTP_PAGE_GENERAL,
    HTTP_PAGE_STATUS,
)

_LOGGER = logging.getLogger(__name__)

HTTP_TIMEOUT = 10.0


class SonanceHttpError(Exception):
    """The HTTP API could not be read."""


@dataclass(frozen=True, slots=True)
class AmplifierIdentity:
    """Identity fields, used for the config entry and DeviceInfo."""

    serial: str
    name: str
    model: str
    firmware: str


@dataclass(frozen=True, slots=True)
class Topology:
    """Channel layout as the amplifier reports it."""

    output_names: list[str]
    input_names: list[str]
    output_groups: list[str]

    def group_name(self, group: int) -> str | None:
        """Derive a zone name from the member channels' output names.

        ``Patio L`` + ``Patio R`` -> ``Patio``. Falls back to the first member's
        name when the common prefix is empty, and to None when the group has no
        members at all.
        """
        letter = GROUP_LETTERS[group].lower()
        members = [
            self.output_names[i]
            for i, g in enumerate(self.output_groups)
            if g.lower() == letter and i < len(self.output_names)
        ]
        if not members:
            return None
        trimmed = [_strip_channel_suffix(m) for m in members]
        first = trimmed[0]
        if all(t == first for t in trimmed) and first:
            return first
        return members[0].strip() or None

    def group_members(self, group: int) -> list[int]:
        letter = GROUP_LETTERS[group].lower()
        return [i for i, g in enumerate(self.output_groups) if g.lower() == letter]


# Longest first, so "Deck Left" is not mistaken for a bare " L" form.
_CHANNEL_SUFFIXES = (" Right", " Left", " L", " R")


def _strip_channel_suffix(name: str) -> str:
    """Drop a trailing L/R channel marker from a channel name.

    Two conventions have to be handled. An installer-assigned name separates
    the marker with a space -- ``Patio L`` -- while the amplifier's factory
    default runs it straight onto the channel number: ``Output 4L``.

    The second case is only stripped when a digit precedes the letter. Without
    that guard, any zone whose name happens to end in L or R would lose its
    last character: ``Pool``, ``Hall``, ``Cellar``, ``XLR``.
    """
    stripped = name.strip()
    for suffix in _CHANNEL_SUFFIXES:
        if stripped.endswith(suffix):
            return stripped[: -len(suffix)].strip()
    if len(stripped) >= 2 and stripped[-1] in "LR" and stripped[-2].isdigit():
        return stripped[:-1].strip()
    return stripped


class SonanceHttpApi:
    """Reads the amplifier's JSON pages. Never writes."""

    def __init__(
        self, session: aiohttp.ClientSession, host: str, port: int = 80
    ) -> None:
        self._session = session
        self._base = f"http://{host}:{port}{HTTP_HANDLER_PATH}"

    async def _read(self, page: str) -> dict[str, Any]:
        # The ``r`` parameter is the web UI's own cache-buster. A fixed value is
        # fine: HA's session does not cache, and a changing one would make
        # request URLs unstable in diagnostics.
        url = f"{self._base}?page={page}&action=read&r=1"
        try:
            async with self._session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT),
                headers={"Accept-Encoding": "gzip"},
            ) as resp:
                resp.raise_for_status()
                # The amplifier serves JSON as text/html, so content_type must
                # not be enforced here.
                return await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise SonanceHttpError(f"Could not read {page}: {err}") from err

    async def identity(self) -> AmplifierIdentity:
        """Read serial, name, model and firmware."""
        data = await self._read(HTTP_PAGE_GENERAL)
        serial = str(data.get("serial-number", "")).strip()
        if not serial:
            raise SonanceHttpError("Amplifier reported no serial number")
        return AmplifierIdentity(
            serial=serial,
            name=str(data.get("amplifier-name", "")).strip() or "Sonance DSP",
            model=str(data.get("amplifier-model", "")).strip() or "Sonance DSP",
            firmware=str(data.get("firmware-version", "")).strip(),
        )

    async def group_power(self) -> dict[int, bool]:
        """Read per-group power.

        The TCP protocol has no group-power query at all, so this endpoint is
        the only source for it.
        """
        data = await self._read(HTTP_PAGE_STATUS)
        states = data.get("power-status") or []
        return {i: str(v).lower() == "on" for i, v in enumerate(states)}

    async def group_mute(self) -> dict[int, bool]:
        data = await self._read(HTTP_PAGE_STATUS)
        states = data.get("mute-volumes") or []
        return {i: str(v).lower() == "on" for i, v in enumerate(states)}

    async def topology(self) -> Topology:
        """Read channel names and the authoritative channel-to-group map."""
        data = await self._read(HTTP_PAGE_BASIC)
        return Topology(
            output_names=[str(n).strip() for n in data.get("output-names") or []],
            input_names=[str(n).strip() for n in data.get("input-names") or []],
            output_groups=[str(g).strip() for g in data.get("output-groups") or []],
        )

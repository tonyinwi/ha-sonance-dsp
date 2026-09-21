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
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from .const import (
    GROUP_LETTERS,
    HTTP_HANDLER_PATH,
    HTTP_PAGE_GENERAL,
    HTTP_PAGE_IN_OUT,
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
    """Channel layout and per-channel settings, as the amplifier reports them.

    Every list is indexed by output channel, 1L..4R, so index 0 is channel 1L
    and index 7 is 4R. ``input_names`` is indexed by INPUT channel on the same
    1L..4R scheme, which is why a source pair maps to indices ``2n`` and
    ``2n+1``.
    """

    output_names: list[str]
    input_names: list[str]
    output_groups: list[str]
    # Per output channel. Read but not yet surfaced -- see the notes on each.
    dsp_presets: list[int] = field(default_factory=list)
    output_volumes: list[str] = field(default_factory=list)
    # The level a channel returns to when the amplifier powers on. The vendor's
    # own integrator notes single this out: the factory default is +12 dB, i.e.
    # maximum gain on every power cycle.
    turn_on_volumes: list[str] = field(default_factory=list)
    # The amplifier's OWN per-channel ceiling, independent of this
    # integration's max_db option. Ours cannot raise a zone above this.
    maximum_volumes: list[str] = field(default_factory=list)
    # Installer calibration, and the reason a dB figure is NOT comparable
    # between zones: two zones at -27 dB with offsets of -6 and +4 are ten dB
    # apart in practice. Anything that averages zone volumes has to say so.
    gain_offset: list[str] = field(default_factory=list)
    level_trim_dbs: list[str] = field(default_factory=list)
    stereo_or_mono: list[str] = field(default_factory=list)
    mode_sources: list[str] = field(default_factory=list)
    # Which INPUT index feeds each output channel's source slot. An assignment,
    # not a selection: which slot is live comes from the TCP source query.
    sources_1: list[int] = field(default_factory=list)
    sources_2: list[int] = field(default_factory=list)

    def group_name(self, group: int) -> str | None:
        """Derive a zone name from the member channels' output names.

        ``Patio L`` + ``Patio R`` -> ``Patio``. Falls back to the first member's
        name when the trimmed names disagree, and to None when the group has no
        members at all.
        """
        members = [
            self.output_names[i]
            for i in self.group_members(group)
            if i < len(self.output_names)
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

    def source_number_for_input_name(self, name: str) -> int | None:
        """Map an input NAME back to its 1-based source number.

        This exists because the TCP source reply cannot be trusted for the
        number. ``Src1=`` is a fixed label: selecting source 2 still answers
        ``Cmd:Source1 ,Group:D Src1=Input 2L``, so the digit is always 1 and
        only the name changes. Verified against all four sources.

        Inputs are stereo pairs -- indices 0/1 are source 1, 2/3 are source 2 --
        and a group query reports its LEFT member, so the name is normally the
        even index of the pair. Both are accepted regardless.
        """
        target = name.strip().casefold()
        for index, candidate in enumerate(self.input_names):
            if candidate.strip().casefold() == target:
                return index // 2 + 1
        return None

    def maximum_db(self, group: int) -> int | None:
        """The lowest device ceiling among a group's channels, in dB."""
        values = [
            int(self.maximum_volumes[i])
            for i in self.group_members(group)
            if i < len(self.maximum_volumes)
        ]
        return min(values) if values else None


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
        """Read the full channel layout from the In/Out Settings endpoint."""
        data = await self._read(HTTP_PAGE_IN_OUT)

        def strings(key: str) -> list[str]:
            return [str(v).strip() for v in data.get(key) or []]

        def ints(key: str) -> list[int]:
            out: list[int] = []
            for v in data.get(key) or []:
                try:
                    out.append(int(v))
                except (TypeError, ValueError):
                    continue
            return out

        return Topology(
            output_names=strings("output-names"),
            input_names=strings("input-names"),
            output_groups=strings("output-groups"),
            dsp_presets=ints("dsp-presets"),
            output_volumes=strings("output-volumes"),
            turn_on_volumes=strings("turn-on-volumes"),
            maximum_volumes=strings("maximum-volumes"),
            gain_offset=strings("gain-offset"),
            level_trim_dbs=strings("level-trim-dBs"),
            stereo_or_mono=strings("stereo-or-mono"),
            mode_sources=strings("mode-sources"),
            sources_1=ints("sources-1"),
            sources_2=ints("sources-2"),
        )

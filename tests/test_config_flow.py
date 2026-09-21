"""Tests for the Sonance DSP config flow.

The Bronze quality scale asks for 100% coverage of the config flow by name, and
this module is the reason: a flow that fails badly is the first thing a user
meets, and the recovery paths are exactly the ones nobody exercises by hand.

Two behavioural assertions here are worth more than the coverage number:

* the duplicate check runs BEFORE the TCP connection test, so re-adding an
  amplifier never disturbs the live entry's single control session
* the control connection is always released, even when connecting raised
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sonance_dsp.config_flow import (
    SonanceConfigFlow,
    SonanceOptionsFlow,
    async_read_identity,
    async_test_control_connection,
)
from custom_components.sonance_dsp.const import (
    CONF_MAX_DB,
    CONF_SCAN_INTERVAL,
    DEFAULT_TCP_PORT,
    DOMAIN,
)
from custom_components.sonance_dsp.http_api import AmplifierIdentity, SonanceHttpError
from custom_components.sonance_dsp.protocol import SonanceConnectionError

IDENTITY = AmplifierIdentity(
    serial="933312106HA0250",
    name="Back Yard",
    model="DSP8-130 MKII",
    firmware="V2.2.8130",
)
USER_INPUT = {CONF_HOST: "192.0.2.10", CONF_PORT: DEFAULT_TCP_PORT}

IDENTITY_PATH = "custom_components.sonance_dsp.config_flow.async_read_identity"
CONTROL_PATH = "custom_components.sonance_dsp.config_flow.async_test_control_connection"
SETUP_PATH = "custom_components.sonance_dsp.async_setup_entry"


def flow_patches(
    identity: Any = IDENTITY,
    identity_error: Exception | None = None,
    control_error: Exception | None = None,
):
    """Patch both validation steps and the entry setup."""
    return (
        patch(
            IDENTITY_PATH,
            side_effect=identity_error,
            return_value=None if identity_error else identity,
        ),
        patch(CONTROL_PATH, side_effect=control_error),
        patch(SETUP_PATH, return_value=True),
    )


async def start_flow(hass: HomeAssistant) -> dict[str, Any]:
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


# ---------------------------------------------------------------------------
# The form itself
# ---------------------------------------------------------------------------


async def test_user_step_shows_a_form_with_no_errors(hass: HomeAssistant) -> None:
    result = await start_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {}


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_creates_entry_titled_from_the_device(hass: HomeAssistant) -> None:
    """The entry is named by the amplifier, and keyed on its serial.

    Keyed on the serial and NOT the host: these amplifiers are commonly on
    DHCP, and an address change must not orphan the entry.
    """
    ident, control, setup = flow_patches()
    result = await start_flow(hass)
    with ident, control, setup as mock_setup:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Back Yard"
    assert result["data"] == USER_INPUT
    assert result["result"].unique_id == IDENTITY.serial
    assert len(mock_setup.mock_calls) == 1


async def test_port_defaults_when_omitted(hass: HomeAssistant) -> None:
    ident, control, setup = flow_patches()
    result = await start_flow(hass)
    with ident, control as mock_control, setup:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.0.2.10"}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_PORT] == DEFAULT_TCP_PORT
    assert mock_control.await_args.args[1] == DEFAULT_TCP_PORT


# ---------------------------------------------------------------------------
# Failure and recovery
#
# Each of these re-submits successfully afterwards. A flow that reports an
# error and then cannot be retried is a flow the user has to abandon.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (SonanceHttpError("boom"), "cannot_connect"),
        (RuntimeError("something else entirely"), "unknown"),
    ],
)
async def test_identity_failure_and_recovery(
    hass: HomeAssistant, error: Exception, expected: str
) -> None:
    result = await start_flow(hass)

    ident, control, setup = flow_patches(identity_error=error)
    with ident, control, setup:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected}

    ident, control, setup = flow_patches()
    with ident, control, setup:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (SonanceConnectionError("refused"), "cannot_connect"),
        (RuntimeError("something else entirely"), "unknown"),
    ],
)
async def test_control_connection_failure_and_recovery(
    hass: HomeAssistant, error: Exception, expected: str
) -> None:
    """HTTP answers but the control port does not.

    Worth its own case: identity comes from HTTP and control from TCP, so an
    amplifier can be perfectly identifiable and still unusable -- most often
    because something else holds its single control session.
    """
    result = await start_flow(hass)

    ident, control, setup = flow_patches(control_error=error)
    with ident, control, setup:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected}

    ident, control, setup = flow_patches()
    with ident, control, setup:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY


# ---------------------------------------------------------------------------
# Duplicates
# ---------------------------------------------------------------------------


async def test_duplicate_serial_aborts(hass: HomeAssistant) -> None:
    MockConfigEntry(
        domain=DOMAIN, unique_id=IDENTITY.serial, data=USER_INPUT, title="Back Yard"
    ).add_to_hass(hass)

    ident, control, setup = flow_patches()
    result = await start_flow(hass)
    with ident, control, setup:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_duplicate_never_opens_the_control_session(
    hass: HomeAssistant,
) -> None:
    """The duplicate check must run BEFORE the TCP test.

    This is the whole reason identity and control are validated separately.
    The amplifier accepts one control session; opening one to validate an entry
    that is about to be rejected would knock the LIVE entry offline. The order
    is load-bearing, and nothing else in the suite would notice it changing.
    """
    MockConfigEntry(
        domain=DOMAIN, unique_id=IDENTITY.serial, data=USER_INPUT, title="Back Yard"
    ).add_to_hass(hass)

    ident, control, setup = flow_patches()
    result = await start_flow(hass)
    with ident, control as mock_control, setup:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )

    assert result["type"] is FlowResultType.ABORT
    mock_control.assert_not_awaited()


async def test_duplicate_refreshes_the_stored_host(hass: HomeAssistant) -> None:
    """A moved amplifier updates the existing entry rather than duplicating it."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=IDENTITY.serial,
        data={CONF_HOST: "192.0.2.99", CONF_PORT: DEFAULT_TCP_PORT},
        title="Back Yard",
    )
    entry.add_to_hass(hass)

    ident, control, setup = flow_patches()
    result = await start_flow(hass)
    with ident, control, setup:
        await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
        await hass.async_block_till_done()

    assert entry.data[CONF_HOST] == "192.0.2.10"


# ---------------------------------------------------------------------------
# The two validation helpers
# ---------------------------------------------------------------------------


async def test_async_read_identity_uses_the_http_api(hass: HomeAssistant) -> None:
    with patch(
        "custom_components.sonance_dsp.config_flow.SonanceHttpApi"
    ) as mock_api:
        mock_api.return_value.identity = AsyncMock(return_value=IDENTITY)
        assert await async_read_identity(hass, "192.0.2.10") is IDENTITY


async def test_control_connection_always_releases_the_session() -> None:
    """Disconnect must run even when connect raised.

    A leaked socket here is the worst case on this device: it holds the only
    control session, so a failed config flow would lock out the setup that
    follows it.
    """
    with patch(
        "custom_components.sonance_dsp.config_flow.SonanceProtocol"
    ) as mock_cls:
        client = mock_cls.return_value
        client.connect = AsyncMock(side_effect=SonanceConnectionError("refused"))
        client.disconnect = AsyncMock()

        with pytest.raises(SonanceConnectionError):
            await async_test_control_connection("192.0.2.10", DEFAULT_TCP_PORT)

        client.disconnect.assert_awaited_once()


async def test_control_connection_releases_on_success() -> None:
    with patch(
        "custom_components.sonance_dsp.config_flow.SonanceProtocol"
    ) as mock_cls:
        client = mock_cls.return_value
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()

        await async_test_control_connection("192.0.2.10", DEFAULT_TCP_PORT)

        client.connect.assert_awaited_once()
        client.disconnect.assert_awaited_once()


# ---------------------------------------------------------------------------
# Options flow
# ---------------------------------------------------------------------------


async def test_options_flow_shows_and_saves(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=IDENTITY.serial, data=USER_INPUT, title="Back Yard"
    )
    entry.add_to_hass(hass)

    with patch(SETUP_PATH, return_value=True):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "init"

        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_MAX_DB: -6, CONF_SCAN_INTERVAL: 15}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_MAX_DB: -6, CONF_SCAN_INTERVAL: 15}


async def test_options_flow_coerces_selector_floats_to_int(
    hass: HomeAssistant,
) -> None:
    """NumberSelector hands back floats; dB and seconds are whole numbers.

    A float reaching the volume mapping would not raise -- it would silently
    produce a fractional dB the device cannot express.
    """
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=IDENTITY.serial, data=USER_INPUT, title="Back Yard"
    )
    entry.add_to_hass(hass)

    with patch(SETUP_PATH, return_value=True):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_MAX_DB: -6.0, CONF_SCAN_INTERVAL: 15.0}
        )
        await hass.async_block_till_done()

    assert entry.options[CONF_MAX_DB] == -6
    assert isinstance(entry.options[CONF_MAX_DB], int)
    assert isinstance(entry.options[CONF_SCAN_INTERVAL], int)


async def test_options_flow_is_reached_from_the_config_flow() -> None:
    assert isinstance(
        SonanceConfigFlow.async_get_options_flow(None), SonanceOptionsFlow
    )

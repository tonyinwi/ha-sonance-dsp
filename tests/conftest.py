"""Shared fixtures for the Sonance DSP integration tests.

Harness: ``pytest-homeassistant-custom-component`` with ``asyncio_mode = auto``
(set in ``pyproject.toml``), so async tests and async fixtures need no marker.

Only fixtures that are actually exercised by the current test suite live here.
The scaffold this replaced also sketched a ``mock_setup_entry`` fixture and a
three-tier fake amplifier; those belong with the tests that need them, and
adding them now would mean shipping fixtures nothing has ever run.
"""

from __future__ import annotations

import sys
from collections.abc import Generator
from pathlib import Path
from unittest.mock import patch

import pytest

# Repo root: ``tests/`` sits directly under it, and ``custom_components/`` is
# its sibling.
REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session", autouse=True)
def expose_custom_components() -> Path:
    """Make the repo's ``custom_components/`` visible to Home Assistant's loader.

    HA finds custom integrations by importing the top-level ``custom_components``
    package and walking its ``__path__`` -- it does *not* look inside
    ``hass.config.config_dir`` for them. So "exposing" the integration means
    nothing more than having the repo root on ``sys.path``.

    pytest's prepend import mode already inserts the repo root here (``tests/``
    has an ``__init__.py``, so the first parent without one is the root), but
    that is a side effect of the package layout rather than a guarantee. Doing
    it explicitly means the suite does not break the day someone deletes
    ``tests/__init__.py`` or runs pytest with ``--import-mode=importlib``.
    """
    root = str(REPO_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    return REPO_ROOT / "custom_components"


@pytest.fixture(autouse=True)
def skip_requirements_install() -> Generator[None]:
    """Stop Home Assistant pip-installing the manifest's requirements.

    Without this, loading the integration makes HA try to resolve
    ``manifest.json`` requirements for real -- a network call, per test, which
    pytest-socket blocks anyway. ``patch`` autodetects the async def and
    supplies an ``AsyncMock``, so awaiting it yields ``True``.
    """
    with patch(
        "homeassistant.requirements.async_process_requirements",
        return_value=True,
    ):
        yield


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(request: pytest.FixtureRequest) -> None:
    """Enable custom integrations, but only for tests that use ``hass``.

    ``enable_custom_integrations`` (from pytest-homeassistant-custom-component)
    pops ``loader.DATA_CUSTOM_COMPONENTS`` so the loader re-scans and picks up
    ``custom_components/sonance_dsp`` -- and it requires the ``hass`` fixture to
    do it.

    Making that autouse unconditionally would spin up a full HomeAssistant
    instance for every test in the suite, including the pure-unit HTTP tests
    that never touch hass. That is slow, and it drags each of them into HA's
    lingering-timer and unclosed-session checks for no benefit. So this pulls
    it in only when the test has already asked for ``hass`` somewhere in its
    fixture closure.
    """
    if "hass" in request.fixturenames:
        request.getfixturevalue("enable_custom_integrations")

"""Preset package -- self-contained preset loading, independent of platform bootstrap.

Imported by ``bos.config.workspace`` at module level so presets are registered
before any config resolution occurs. Third-party packages register presets via
the ``bos.config.presets`` entry-point group.
"""

from __future__ import annotations

# Third-party entry point presets
from importlib.metadata import entry_points
from logging import getLogger

# Built-in presets (register via @ep_preset at import time)
# Python presets imported first — TOML helper skips already-registered names
from . import (
    _toml_helper,  # noqa: F401 — side-effect: registers TOML presets
    default,  # noqa: F401 — side-effect: registers default preset
)

logger = getLogger(__name__)
for ep in entry_points(group="bos.config.presets"):
    try:
        ep.load()
    except Exception:
        logger.warning("Failed to load preset entry point %s", ep.name, exc_info=True)

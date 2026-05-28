"""Preset package -- registers preset agent specs into ``ep_agent_spec``.

Imported by ``bos.config.workspace`` at module level so presets are registered
before any config resolution occurs. ``bos.exts`` also imports this module so
agent spec extensions are available when the platform boots.

Third-party packages register presets via the ``bos.config.presets`` entry-point
group.
"""

from __future__ import annotations

from importlib.metadata import entry_points
from logging import getLogger

from bos.core.contract import ep_agent_spec
from bos.core.registry import Extension

from .default import get_default_agent_spec

logger = getLogger(__name__)

# ── Register built-in presets ────────────────────────────────────────────────

ep_agent_spec.register(
    Extension(
        name="_default",
        fn=get_default_agent_spec,
        description="Built-in default agent spec.",
    )
)

# ── Load third-party preset entry points ─────────────────────────────────────

for ep in entry_points(group="bos.config.presets"):
    try:
        ep.load()
    except Exception:
        logger.warning("Failed to load preset entry point %s", ep.name, exc_info=True)

"""Register existing TOML presets as ep_preset entries."""

import logging
import tomllib
from pathlib import Path
from typing import Any

from bos.core.contract import ep_preset
from bos.core.registry import Extension

logger = logging.getLogger(__name__)

_PRESETS_DIR = Path(__file__).resolve().parent


class _TomlPreset:
    """Preset backed by a TOML file."""

    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config

    def get_agent_spec(self) -> dict[str, Any]:
        platform = self._config.get("platform", {})
        agents = platform.get("agents", [])
        if agents:
            return agents[0]
        agent_defaults = platform.get("agent_defaults", {})
        return {"name": "_default"} | agent_defaults


def _register_toml_presets() -> None:
    if not _PRESETS_DIR.exists():
        return
    for toml_path in _PRESETS_DIR.glob("*.toml"):
        name = toml_path.stem
        if ep_preset.has(name):
            continue
        try:
            config = tomllib.loads(toml_path.read_text("utf-8"))
        except Exception:
            logger.warning("Failed to load TOML preset %s", toml_path, exc_info=True)
            continue

        def _make_factory(cfg: dict[str, Any]):
            def _factory() -> _TomlPreset:
                return _TomlPreset(cfg)

            return _factory

        ep_preset.register(
            Extension(
                name=name,
                fn=_make_factory(config),
                description=f"TOML preset: {toml_path}",
                defaults={},
                metadata={"source": str(toml_path)},
            )
        )


_register_toml_presets()
del _register_toml_presets

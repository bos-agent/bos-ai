from __future__ import annotations

import os
import shutil
import tomllib
from pathlib import Path
from typing import Any


def _load_config(workspace: str | Path = ".") -> tuple[Path, dict[str, Any]]:
    workspace = Path(workspace).expanduser().resolve()
    bos_dir = None
    for parent in [workspace] + list(workspace.parents):
        if (parent / ".bos").exists():
            bos_dir = parent / ".bos"
            break
    else:
        bos_dir = Path(os.environ.get("BOS_DIR", "~/.bos")).expanduser()

    cfg_file = bos_dir / "config.toml"
    if not cfg_file.exists():
        bos_dir.mkdir(parents=True, exist_ok=True)
        return bos_dir, {}
    return bos_dir, tomllib.loads(cfg_file.read_text(encoding="utf-8"))


class Workspace:
    def __init__(self, workspace: str | Path = "."):
        self.workspace = Path(workspace).expanduser().resolve()
        self.bos_dir, self.config = _load_config(self.workspace)

    def init(self):
        self.bos_dir.mkdir(parents=True, exist_ok=True)
        cfg_file = self.bos_dir / "config.toml"
        if cfg_file.exists():
            raise FileExistsError(f"Config file {cfg_file} already exists.")
        config_template_path = Path(__file__).resolve().parents[1] / "config_template.toml"
        shutil.copy2(config_template_path, cfg_file)
        self.config = tomllib.loads(cfg_file.read_text(encoding="utf-8"))

    def bootstrap_platform(self):
        from bos.core import _apply, bootstrap_platform

        platform_cfg = self.config.get("platform", {}) | {"bos_dir": self.bos_dir}
        _apply(bootstrap_platform, platform_cfg)

    def harness(self):
        from bos.core import AgentHarness, _apply

        harness_cfg = self.config.get("harness", {}) | {"bos_dir": self.bos_dir, "workspace": self.workspace}
        return _apply(AgentHarness, harness_cfg)

    def enable_interceptors(self, interceptors: list[str | dict[str, Any]]):
        interceptors_cfg = self.config.setdefault("harness", {}).setdefault("interceptors", [])
        interceptors_cfg.extend(i for i in interceptors if i not in interceptors_cfg)

    def get_setting(self, key: str):
        settings, segments = self.config, key.split(".")
        for seg in segments[:-1]:
            settings = settings.get(seg, {})
        return settings.get(segments[-1])

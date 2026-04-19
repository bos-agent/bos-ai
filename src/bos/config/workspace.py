from __future__ import annotations

import os
import shutil
import tomllib
from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class AgentRuntimeConfig:
    kind: str = "process"
    image: str | None = None
    container_name: str | None = None
    workspace_dir: str = "/workspace"
    bos_dir: str | None = None


@dataclass(frozen=True)
class ResolvedChannelConfig:
    name: str
    bind_address: str
    target_address: str
    options: dict[str, Any] = field(default_factory=dict)

    def extension_config(self) -> dict[str, Any]:
        return {"name": self.name, "target_address": self.target_address} | self.options


class Workspace:
    def __init__(self, workspace: str | Path = "."):
        self.workspace = Path(workspace).expanduser().resolve()
        self.bos_dir, self.config = _load_config(self.workspace)

    def init(self):
        self.bos_dir.mkdir(parents=True, exist_ok=True)
        cfg_file = self.bos_dir / "config.toml"
        if cfg_file.exists():
            raise FileExistsError(f"Config file {cfg_file} already exists.")
        config_template_path = Path(__file__).resolve().parent / "template.toml"
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

    def get_main_agent_name(self) -> str:
        return self.get_setting("main.agent") or "_default"

    def get_main_agent_address(self) -> str:
        return "agent@main"

    def get_runtime_config(self, *, force_kind: str | None = None) -> AgentRuntimeConfig:
        runtime_cfg = self.config.get("main", {}).get("runtime", {})
        workspace_dir = runtime_cfg.get("workspace_dir") or "/workspace"
        bos_dir = runtime_cfg.get("bos_dir")
        if not bos_dir:
            try:
                bos_rel = self.bos_dir.relative_to(self.workspace)
                bos_dir = str((Path(workspace_dir) / bos_rel).as_posix())
            except ValueError:
                bos_dir = "/bos"

        return AgentRuntimeConfig(
            kind=force_kind or runtime_cfg.get("kind") or "process",
            image=runtime_cfg.get("image"),
            container_name=runtime_cfg.get("container_name"),
            workspace_dir=str(Path(workspace_dir).as_posix()),
            bos_dir=str(Path(bos_dir).as_posix()),
        )

    def resolve_platform_envfile(self) -> Path | None:
        envfile = self.config.get("platform", {}).get("envfile")
        if not envfile:
            return None
        return (self.bos_dir / Path(envfile).expanduser()).resolve()

    def resolve_channels(self, *, runtime_kind: str = "process") -> list[ResolvedChannelConfig]:
        actor_address = self.get_main_agent_address()
        raw_channels = self.config.get("main", {}).get("channels") or [
            {
                "name": "HttpChannel",
                "bind_address": "channel@http",
                "target_address": actor_address,
            }
        ]
        channels: list[ResolvedChannelConfig] = []
        seen_bind_addresses: set[str] = set()

        for idx, raw_cfg in enumerate(raw_channels, start=1):
            if not isinstance(raw_cfg, dict):
                raise ValueError(f"Channel entry #{idx} must be a table, got {type(raw_cfg).__name__}.")

            name = str(raw_cfg.get("name") or "_default")
            bind_address = str(raw_cfg.get("bind_address") or "").strip()
            if not bind_address:
                raise ValueError(f"Channel {name!r} must define bind_address.")
            if not bind_address.startswith("channel@"):
                raise ValueError(f"Channel {name!r} bind_address must start with 'channel@': {bind_address!r}")
            if bind_address in seen_bind_addresses:
                raise ValueError(f"Duplicate channel bind_address: {bind_address!r}")
            seen_bind_addresses.add(bind_address)

            target_address = str(raw_cfg.get("target_address") or actor_address).strip()
            options = self._normalize_channel_options(
                {
                    key: value
                    for key, value in raw_cfg.items()
                    if key not in {"name", "bind_address", "target_address"}
                },
                name=name,
                runtime_kind=runtime_kind,
            )
            channels.append(
                ResolvedChannelConfig(
                    name=name,
                    bind_address=bind_address,
                    target_address=target_address,
                    options=options,
                )
            )

        self._validate_channel_topology(channels, actor_address=actor_address)
        return channels

    @staticmethod
    def _normalize_channel_options(options: dict[str, Any], *, name: str, runtime_kind: str) -> dict[str, Any]:
        normalized = dict(options)
        if runtime_kind == "docker" and name == "HttpChannel":
            host = normalized.get("host")
            if host in (None, "", "127.0.0.1", "localhost"):
                normalized["host"] = "0.0.0.0"
        return normalized

    @staticmethod
    def _validate_channel_topology(channels: list[ResolvedChannelConfig], *, actor_address: str) -> None:
        channel_names_by_address = {channel.bind_address: channel.name for channel in channels}
        for channel in channels:
            if channel.target_address == channel.bind_address:
                raise ValueError(f"Channel {channel.bind_address!r} cannot target itself.")

            if channel.target_address.startswith("agent@"):
                if channel.target_address != actor_address:
                    raise ValueError(
                        f"Channel {channel.bind_address!r} targets unknown actor address {channel.target_address!r}."
                    )
                continue

            if not channel.target_address.startswith("channel@"):
                raise ValueError(
                    f"Channel {channel.bind_address!r} target_address must start with 'agent@' or 'channel@'."
                )

            target_channel_name = channel_names_by_address.get(channel.target_address)
            if target_channel_name is None:
                raise ValueError(
                    f"Channel {channel.bind_address!r} targets unknown channel address {channel.target_address!r}."
                )
            if channel.name == "BroadcastChannel":
                raise ValueError(
                    f"BroadcastChannel {channel.bind_address!r} must target an actor address, "
                    f"got {channel.target_address!r}."
                )
            if target_channel_name != "BroadcastChannel":
                raise ValueError(
                    f"Channel {channel.bind_address!r} must target an actor or BroadcastChannel address, "
                    f"got {channel.target_address!r}."
                )

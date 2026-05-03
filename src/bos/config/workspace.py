from __future__ import annotations

import copy
import logging
import os
import re
import shutil
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from bos.core import AgentHarness, _apply


class WorkspaceResolutionError(RuntimeError):
    """Raised when the active BOS config source cannot be resolved unambiguously."""


@dataclass(frozen=True)
class AgentSourceRecord:
    name: str
    source_kind: Literal["inline", "file"]
    source_path: str
    load_order: int
    won: bool = False


@dataclass(frozen=True)
class _LoadedAgentCandidate:
    spec: dict[str, Any]
    source_kind: Literal["inline", "file"]
    source_path: str
    load_order: int


_EXTERNAL_AGENT_SUFFIXES = {".toml", ".md"}
_FRONTMATTER_ALIAS_KEYS = {
    # Keep this compatibility typo narrow: misspelling it otherwise creates an
    # unusable ReactAgent default that fails only at agent construction time.
    "exlude_tools": "exclude_tools",
}


@dataclass(frozen=True)
class _DiscoveredConfig:
    """Result of workspace filesystem discovery."""

    bos_dir: Path
    config_file: Path


def _resolve_workspace_path(workspace: str | Path = ".") -> Path:
    return Path(workspace).expanduser().resolve()


def _find_discovered_config(workspace: Path) -> _DiscoveredConfig | None:
    for parent in [workspace] + list(workspace.parents):
        has_dotbos = (parent / ".bos").exists()
        has_bostoml = (parent / "bos.toml").is_file()

        if has_dotbos and has_bostoml:
            raise WorkspaceResolutionError(
                f"Ambiguous BOS config: found both .bos/ and bos.toml in {parent}. Remove one to resolve the ambiguity."
            )

        if has_dotbos:
            return _DiscoveredConfig(
                bos_dir=parent / ".bos",
                config_file=parent / ".bos" / "config.toml",
            )

        if has_bostoml:
            return _DiscoveredConfig(
                bos_dir=parent,
                config_file=parent / "bos.toml",
            )

    return None


def _resolve_config(workspace: Path) -> _DiscoveredConfig:
    discovered = _find_discovered_config(workspace)
    configured_bos_dir = os.environ.get("BOS_DIR")
    env_bos_dir = Path(configured_bos_dir).expanduser().resolve() if configured_bos_dir else None

    if discovered and env_bos_dir:
        resolved_bos_dir = discovered.bos_dir.resolve()
        if resolved_bos_dir != env_bos_dir:
            raise WorkspaceResolutionError(
                f"Ambiguous BOS config: discovered {resolved_bos_dir} and BOS_DIR={env_bos_dir}. "
                "Unset BOS_DIR or run outside that workspace."
            )
        return _DiscoveredConfig(bos_dir=resolved_bos_dir, config_file=discovered.config_file.resolve())

    if discovered:
        return _DiscoveredConfig(
            bos_dir=discovered.bos_dir.resolve(),
            config_file=discovered.config_file.resolve(),
        )
    if env_bos_dir:
        return _DiscoveredConfig(bos_dir=env_bos_dir, config_file=env_bos_dir / "config.toml")

    raise WorkspaceResolutionError("No BOS workspace found. Run `bos init`, `cd` into a workspace, or set `BOS_DIR`.")


def _config_template_path() -> Path:
    return Path(__file__).resolve().parent / "template.toml"


def _load_config(workspace: str | Path = ".") -> tuple[Path, dict[str, Any]]:
    workspace = _resolve_workspace_path(workspace)
    resolved = _resolve_config(workspace)
    if not resolved.config_file.exists():
        return resolved.bos_dir, {}
    return resolved.bos_dir, tomllib.loads(resolved.config_file.read_text(encoding="utf-8"))


def initialize_workspace(workspace: str | Path = ".", *, dotbos: bool = False) -> Path:
    workspace = _resolve_workspace_path(workspace)

    existing = _find_discovered_config(workspace)
    if existing is not None:
        raise WorkspaceResolutionError(
            f"Workspace already initialized: found {existing.config_file}. Remove it before re-initializing."
        )

    if dotbos:
        bos_dir = workspace / ".bos"
        cfg_file = bos_dir / "config.toml"
        bos_dir.mkdir(parents=True, exist_ok=True)
    else:
        bos_dir = workspace
        cfg_file = workspace / "bos.toml"

    shutil.copy2(_config_template_path(), cfg_file)
    return bos_dir


def _load_external_agent_markdown(content: str, *, source_path: str) -> dict[str, Any]:
    try:
        frontmatter, system_prompt = _split_markdown_frontmatter(content, source_path=source_path)
        spec = _parse_frontmatter(frontmatter, source_path=source_path)
    except ValueError as exc:
        logging.getLogger(__name__).warning(
            "Invalid frontmatter in Markdown agent definition %s; using the whole file as system_prompt: %s",
            source_path,
            exc,
        )
        return {"system_prompt": content}

    if "system_prompt" in spec:
        logging.getLogger(__name__).warning(
            "Markdown agent definition %s defines system_prompt in frontmatter; using the whole file as system_prompt.",
            source_path,
        )
        return {"system_prompt": content}

    spec["system_prompt"] = system_prompt
    return spec


def _split_markdown_frontmatter(content: str, *, source_path: str) -> tuple[str, str]:
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return "", content

    for idx, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return "".join(lines[1:idx]), "".join(lines[idx + 1 :])

    raise ValueError(f"Invalid Markdown agent definition {source_path}: frontmatter is missing closing '---'.")


def _parse_frontmatter(frontmatter: str, *, source_path: str) -> dict[str, Any]:
    if not frontmatter.strip():
        return {}

    try:
        return _parse_simple_yaml_mapping(frontmatter)
    except ValueError as exc:
        raise ValueError(f"Invalid frontmatter in agent definition {source_path}: {exc}") from exc


def _parse_simple_yaml_mapping(frontmatter: str) -> dict[str, Any]:
    lines = frontmatter.splitlines()
    result: dict[str, Any] = {}
    idx = 0

    while idx < len(lines):
        raw_line = lines[idx]
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            idx += 1
            continue
        if raw_line[:1].isspace():
            raise ValueError(f"unexpected indented line {idx + 1}")

        key, separator, raw_value = stripped.partition(":")
        if not separator or not key.strip():
            raise ValueError(f"line {idx + 1} must be a key-value pair")
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", key):
            raise ValueError(f"unsupported key {key!r} on line {idx + 1}")

        value = raw_value.strip()
        if value in {"|", ">"}:
            block_lines, idx = _collect_indented_block(lines, idx + 1)
            result[key] = _parse_block_scalar(block_lines, folded=value == ">")
        elif value:
            result[key] = _parse_frontmatter_scalar(value)
            idx += 1
        else:
            block_lines, next_idx = _collect_indented_block(lines, idx + 1)
            result[key] = _parse_frontmatter_block(block_lines)
            idx = next_idx

    for alias, canonical in _FRONTMATTER_ALIAS_KEYS.items():
        if alias in result:
            alias_value = result.pop(alias)
            result.setdefault(canonical, alias_value)

    return result


def _collect_indented_block(lines: list[str], start_idx: int) -> tuple[list[str], int]:
    block_lines: list[str] = []
    idx = start_idx

    while idx < len(lines):
        line = lines[idx]
        stripped = line.strip()
        if not stripped:
            block_lines.append(line)
            idx += 1
            continue
        if not line[:1].isspace():
            break
        block_lines.append(line)
        idx += 1

    return block_lines, idx


def _parse_frontmatter_block(lines: list[str]) -> Any:
    meaningful_lines = [line for line in lines if line.strip() and not line.strip().startswith("#")]
    if not meaningful_lines:
        return None

    if all(line.lstrip().startswith("- ") for line in meaningful_lines):
        return [_parse_frontmatter_scalar(line.lstrip()[2:].strip()) for line in meaningful_lines]

    mapping: dict[str, Any] = {}
    for line in meaningful_lines:
        stripped = line.strip()
        key, separator, raw_value = stripped.partition(":")
        if not separator or not key.strip():
            raise ValueError(f"unsupported nested frontmatter block line {stripped!r}")
        mapping[key.strip()] = _parse_frontmatter_scalar(raw_value.strip()) if raw_value.strip() else None
    return mapping


def _parse_block_scalar(lines: list[str], *, folded: bool) -> str:
    if not lines:
        return ""

    indent = min((len(line) - len(line.lstrip(" ")) for line in lines if line.strip()), default=0)
    normalized_lines = [line[indent:] if len(line) >= indent else "" for line in lines]
    if folded:
        return " ".join(line.strip() for line in normalized_lines if line.strip())
    return "\n".join(line.rstrip() for line in normalized_lines)


def _parse_frontmatter_scalar(value: str) -> Any:
    value = _strip_inline_comment(value.strip())
    if _is_comment_or_empty(value):
        return None
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_parse_frontmatter_scalar(item) for item in _split_inline_list(inner)]
    if value[:1] == value[-1:] and value[:1] in {"'", '"'}:
        return value[1:-1]

    lower_value = value.lower()
    if lower_value in {"true", "false"}:
        return lower_value == "true"
    if lower_value in {"null", "none", "~"}:
        return None
    if re.fullmatch(r"[+-]?\d+", value):
        return int(value)
    if re.fullmatch(r"[+-]?(?:\d+\.\d*|\.\d+)", value):
        return float(value)
    return value


def _split_inline_list(value: str) -> list[str]:
    items: list[str] = []
    current: list[str] = []
    quote: str | None = None
    escaped = False

    for char in value:
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == "\\" and quote == '"':
            current.append(char)
            escaped = True
            continue
        if char in {"'", '"'}:
            quote = None if quote == char else char if quote is None else quote
            current.append(char)
            continue
        if char == "," and quote is None:
            items.append("".join(current).strip())
            current = []
            continue
        current.append(char)

    if quote is not None:
        raise ValueError("unterminated quoted string in inline list")

    items.append("".join(current).strip())
    return items


def _strip_inline_comment(value: str) -> str:
    quote: str | None = None
    for idx, char in enumerate(value):
        if char in {"'", '"'}:
            quote = None if quote == char else char if quote is None else quote
        if char == "#" and quote is None and (idx == 0 or value[idx - 1].isspace()):
            return value[:idx].rstrip()
    return value


def _is_comment_or_empty(value: str) -> bool:
    return not value or value.startswith("#")


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
        self.workspace = _resolve_workspace_path(workspace)
        self.bos_dir, self.config = _load_config(self.workspace)
        self.agent_source_history: dict[str, list[AgentSourceRecord]] = {}

    def bootstrap_platform(self):
        from bos.core import _apply, bootstrap_platform

        platform_cfg = self.resolve_platform_config() | {"bos_dir": self.bos_dir}
        _apply(bootstrap_platform, platform_cfg)

    def resolve_platform_config(self) -> dict[str, Any]:
        raw_platform_cfg = self.config.get("platform", {})
        if not isinstance(raw_platform_cfg, dict):
            raise ValueError("[platform] must be a table.")

        resolved_platform_cfg = copy.deepcopy({k: v for k, v in raw_platform_cfg.items() if k != "agent_dirs"})

        if resolved_platform_cfg.get("extensions") is None:
            resolved_platform_cfg["extensions"] = ["bos.extensions.all", "./extensions"]

        if resolved_platform_cfg.get("agent_dirs") is None:
            resolved_platform_cfg["agent_dirs"] = ["./agents"]

        try:
            resolved_agents, source_history = self._resolve_platform_agents(raw_platform_cfg)
        except Exception:
            self.agent_source_history = {}
            raise
        self.agent_source_history = source_history

        if "agents" in raw_platform_cfg or raw_platform_cfg.get("agent_dirs") is not None or resolved_agents:
            resolved_platform_cfg["agents"] = resolved_agents

        return resolved_platform_cfg

    def harness(self) -> AgentHarness:
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
                {key: value for key, value in raw_cfg.items() if key not in {"name", "target_address"}},
                name=name,
                runtime_kind=runtime_kind,
            ) | {
                "bind_address": bind_address,
                "bos_dir": str(self.bos_dir),
                "workspace_dir": str(self.workspace),
            }
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
    def _normalize_channel_options(
        options: dict[str, Any],
        *,
        name: str,
        runtime_kind: str,
    ) -> dict[str, Any]:
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
            if channel.name == "BroadcastChannel":
                raise ValueError("BroadcastChannel is no longer supported; configure channels to target agent@main.")
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

            if channel.target_address not in channel_names_by_address:
                raise ValueError(
                    f"Channel {channel.bind_address!r} targets unknown channel address {channel.target_address!r}."
                )
            raise ValueError(
                f"Channel {channel.bind_address!r} must target the actor address {actor_address!r}; "
                f"channel-to-channel routing is no longer supported."
            )

    def _resolve_platform_agents(
        self,
        raw_platform_cfg: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], dict[str, list[AgentSourceRecord]]]:
        candidates: list[_LoadedAgentCandidate] = []
        load_order = 0

        raw_inline_agents = raw_platform_cfg.get("agents")
        if raw_inline_agents is None:
            raw_inline_agents = []
        elif not isinstance(raw_inline_agents, list):
            raise ValueError("platform.agents must be a list of tables.")

        for idx, raw_agent in enumerate(raw_inline_agents, start=1):
            load_order += 1
            candidates.append(self._load_inline_agent_candidate(raw_agent, idx=idx, load_order=load_order))

        for candidate_path in self._discover_external_agent_candidates(raw_platform_cfg):
            load_order += 1
            candidates.append(self._load_external_agent_candidate(candidate_path, load_order=load_order))

        return self._merge_agent_candidates(candidates)

    def _load_inline_agent_candidate(self, raw_agent: Any, *, idx: int, load_order: int) -> _LoadedAgentCandidate:
        if not isinstance(raw_agent, dict):
            raise ValueError(f"platform.agents[{idx}] must be a table, got {type(raw_agent).__name__}.")

        spec = copy.deepcopy(raw_agent)
        source_path = "config.toml"
        self._normalize_agent_name(spec, source_path=source_path)
        self._normalize_agent_system_prompt(spec, source_path=source_path)

        return _LoadedAgentCandidate(
            spec=spec,
            source_kind="inline",
            source_path=source_path,
            load_order=load_order,
        )

    def _discover_external_agent_candidates(self, raw_platform_cfg: dict[str, Any]) -> list[Path]:
        raw_agent_dirs = raw_platform_cfg.get("agent_dirs", ["agents"])
        if not isinstance(raw_agent_dirs, list):
            raise ValueError("platform.agent_dirs must be a list of strings.")

        candidates: list[Path] = []
        for raw_dir in raw_agent_dirs:
            if not isinstance(raw_dir, str) or not raw_dir.strip():
                raise ValueError("Each entry in platform.agent_dirs must be a non-empty string.")

            # Resolve: expand ~, then resolve relative paths against .bos/
            agents_root = Path(raw_dir.strip()).expanduser()
            if not agents_root.is_absolute():
                agents_root = (self.bos_dir / agents_root).resolve()
            else:
                agents_root = agents_root.resolve()

            if not agents_root.exists() or not agents_root.is_dir():
                logging.getLogger(__name__).warning(
                    "platform.agent_dirs entry %r is not a directory (%s), skipping.",
                    raw_dir,
                    agents_root,
                )
                continue

            dir_candidates: list[Path] = []
            for entry in agents_root.iterdir():
                if entry.is_file() and entry.suffix.lower() in _EXTERNAL_AGENT_SUFFIXES:
                    dir_candidates.append(entry)

            # Sort alphabetically within each directory
            candidates.extend(sorted(dir_candidates, key=lambda p: p.name))

        return candidates

    def _load_external_agent_candidate(self, candidate_path: Path, *, load_order: int) -> _LoadedAgentCandidate:
        candidate_path = candidate_path.resolve()
        try:
            source_path = candidate_path.relative_to(self.bos_dir).as_posix()
        except ValueError:
            source_path = candidate_path.as_posix()
        content = candidate_path.read_text(encoding="utf-8")
        if candidate_path.suffix.lower() == ".md":
            spec = _load_external_agent_markdown(content, source_path=source_path)
        else:
            try:
                spec = tomllib.loads(content)
            except tomllib.TOMLDecodeError as exc:
                raise ValueError(f"Invalid TOML in agent definition {source_path}: {exc}") from exc

        if not isinstance(spec, dict):
            raise ValueError(f"Agent definition {source_path} must be a table.")

        spec = copy.deepcopy(spec)
        self._normalize_external_agent_name(spec, candidate_path=candidate_path, source_path=source_path)
        self._normalize_agent_system_prompt(spec, source_path=source_path)

        return _LoadedAgentCandidate(
            spec=spec,
            source_kind="file",
            source_path=source_path,
            load_order=load_order,
        )

    @staticmethod
    def _normalize_agent_name(spec: dict[str, Any], *, source_path: str) -> None:
        name = spec.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"Agent definition {source_path} must define a non-empty name.")
        spec["name"] = name.strip()

    @staticmethod
    def _normalize_external_agent_name(spec: dict[str, Any], *, candidate_path: Path, source_path: str) -> None:
        """Use explicit name if provided; otherwise derive from filename stem."""
        explicit_name = spec.get("name")
        if isinstance(explicit_name, str) and explicit_name.strip():
            spec["name"] = explicit_name.strip()
            return

        # Derive name from filename stem: <agent_dir>/<name>.toml → <name>
        spec["name"] = candidate_path.stem

    @staticmethod
    def _normalize_agent_system_prompt(spec: dict[str, Any], *, source_path: str) -> None:
        if "system_prompt" not in spec:
            return
        system_prompt = spec["system_prompt"]
        if system_prompt is None:
            return
        if not isinstance(system_prompt, str):
            raise ValueError(
                f"Agent definition {source_path} system_prompt must be a string. "
                "Nested system_prompt tables are no longer supported; use a multiline string."
            )

    def _merge_agent_candidates(
        self,
        candidates: list[_LoadedAgentCandidate],
    ) -> tuple[list[dict[str, Any]], dict[str, list[AgentSourceRecord]]]:
        exact_name_histories: dict[str, list[AgentSourceRecord]] = {}
        exact_name_specs: dict[str, tuple[int, dict[str, Any]]] = {}
        casefold_sources: dict[str, _LoadedAgentCandidate] = {}

        for candidate in candidates:
            name = str(candidate.spec["name"])
            name_key = name.casefold()
            existing_case_candidate = casefold_sources.get(name_key)
            if existing_case_candidate is not None and existing_case_candidate.spec["name"] != name:
                raise ValueError(
                    "Case-only agent name collision: "
                    f"{existing_case_candidate.spec['name']!r} from {existing_case_candidate.source_path} "
                    f"conflicts with {name!r} from {candidate.source_path}."
                )

            casefold_sources.setdefault(name_key, candidate)
            exact_name_histories.setdefault(name, []).append(
                AgentSourceRecord(
                    name=name,
                    source_kind=candidate.source_kind,
                    source_path=candidate.source_path,
                    load_order=candidate.load_order,
                    won=False,
                )
            )
            exact_name_specs[name] = (candidate.load_order, copy.deepcopy(candidate.spec))

        source_history: dict[str, list[AgentSourceRecord]] = {}
        for name, history in exact_name_histories.items():
            source_history[name] = [
                record if idx != len(history) - 1 else replace(record, won=True) for idx, record in enumerate(history)
            ]

        resolved_agents = [spec for _, spec in sorted(exact_name_specs.values(), key=lambda item: item[0])]
        return resolved_agents, source_history

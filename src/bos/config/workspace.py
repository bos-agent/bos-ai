from __future__ import annotations

import copy
import logging
import os
import re
import shutil
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from bos.config.schema import (
    AgentConfig,
    RootConfig,
    _agent_config_to_core_kwargs,
    validate_agent_config,
    validate_config,
)
from bos.core import DEFAULT_AGENT_KIND, AgentHarness, _deep_merge, _get_bos_home, _resolve_path

if TYPE_CHECKING:
    # The gateway owns these config *shapes* (BEP 13 §3.3); the loader (this ring)
    # imports them only to produce them. Constructed via lazy imports inside the
    # resolve_* methods so ``import bos.config`` stays light (the gateway package
    # pulls aiohttp); annotations are strings via ``from __future__ import annotations``.
    from bos.gateway.config import (
        GatewayRuntimeConfig,
        ResolvedActorConfig,
        ResolvedGatewayChannelConfig,
        ResolvedGatewayConfig,
    )


class WorkspaceResolutionError(RuntimeError):
    """Raised when the active BOS config source cannot be resolved unambiguously."""


class ConfigNotFoundError(WorkspaceResolutionError):
    """Raised when no BOS config file is found in the workspace tree."""


class ConfigValidationError(WorkspaceResolutionError):
    """Raised when the BOS config file fails Pydantic validation."""


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


def find_discovered_config(workspace: Path) -> Path | None:
    for parent in [workspace] + list(workspace.parents):
        dotbos_config = parent / ".bos" / "config.toml"
        if dotbos_config.is_file():
            return dotbos_config
        # .bos/ may exist without config.toml — keep walking up

    return None


def _resolve_config(workspace: Path) -> Path:
    """Walk ancestor directories to find the BOS config file.

    Checks ``BOS_CONFIG`` env var and raises on ambiguity.
    """
    discovered_config = find_discovered_config(workspace)
    configured_bos_config = os.environ.get("BOS_CONFIG")
    env_bos_config = Path(configured_bos_config).expanduser().resolve() if configured_bos_config else None

    if discovered_config and env_bos_config:
        if discovered_config.resolve() != env_bos_config:
            raise WorkspaceResolutionError(
                f"Ambiguous BOS config: discovered {discovered_config.resolve()} and BOS_CONFIG={env_bos_config}. "
                "Unset BOS_CONFIG or run outside that workspace."
            )
        return discovered_config.resolve()

    if discovered_config:
        return discovered_config.resolve()
    if env_bos_config:
        return env_bos_config

    raise ConfigNotFoundError("No BOS workspace found. Run `boscli init`, `cd` into a workspace, or set `BOS_CONFIG`.")


def _config_template_path() -> Path:
    return Path(__file__).resolve().parent / "template.toml"


def _load_config(config_path: Path | str) -> dict[str, Any]:
    """Read and parse a TOML config file. Returns ``{}`` if the file does not exist."""
    if not config_path:
        raise WorkspaceResolutionError("Config path is not resolved.")

    config_path = Path(config_path)

    if not config_path.is_file():
        return {}
    return tomllib.loads(config_path.read_text(encoding="utf-8"))


def presets_dir() -> Path:
    return Path(__file__).resolve().parent / "presets"


def resolve_config_source(config_arg: str) -> tuple[Path, Path, RootConfig]:
    """Resolve a ``-c`` / ``--config`` argument to ``(config_path, bos_dir, config)``.

    * If *config_arg* is an existing file path → ``bos_dir`` is the file's parent.
    * If *config_arg* matches a built-in preset name → ``bos_dir`` is
      ``~/.bos/presets/<preset>`` (created if necessary).

    Returns validated :class:`RootConfig`. Raises :class:`WorkspaceResolutionError`
    if the source cannot be resolved.
    """
    config_path = Path(config_arg)
    if config_path.is_file():
        resolved = config_path.resolve()
        raw = _load_config(resolved)
        return resolved, resolved.parent, validate_config(raw)

    presets = presets_dir()
    preset = presets / f"{config_arg}.toml"
    if preset.exists():
        bos_dir = _get_bos_home() / "presets" / config_arg
        bos_dir.mkdir(parents=True, exist_ok=True)
        raw = _load_config(preset)
        return preset, bos_dir, validate_config(raw)

    available = sorted(p.stem for p in presets.glob("*.toml")) if presets.exists() else []
    presets_msg = f"Available presets: {', '.join(available)}" if available else "No presets available"
    raise WorkspaceResolutionError(f"Unknown config source {config_arg!r}. {presets_msg}")


def initialize_workspace(workspace: str | Path = ".") -> Path:
    workspace = _resolve_path(workspace)

    existing_config = find_discovered_config(workspace)
    if existing_config is not None:
        raise WorkspaceResolutionError(
            f"Workspace already initialized: found {existing_config}. Remove it before re-initializing."
        )

    bos_dir = workspace / ".bos"
    cfg_file = bos_dir / "config.toml"
    bos_dir.mkdir(parents=True, exist_ok=True)

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


def _run_coro_blocking(coro: Any) -> Any:
    """Drive *coro* to completion from synchronous code, with or without a loop.

    ``bootstrap_platform`` is a synchronous entrypoint, but it is sometimes
    called from inside a running event loop (async tests, embedding). A bare
    ``asyncio.run`` raises there, so when a loop is already running we run the
    coroutine on a dedicated worker thread that owns its own loop.
    """
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(coro)).result()


def _invoke_agent_factories() -> dict[str, dict[str, Any]]:
    """Invoke each ``ep_agent`` spec factory once and validate its output.

    Factories receive their merged ``[exts.ep_agent.<name>]`` defaults as
    keyword arguments and must return a dict validatable by ``AgentConfig``
    (the same shape as a ``[agents.<name>]`` TOML table). ``invoke`` is async
    (sync and async factories alike); the calls are driven synchronously via
    :func:`_run_coro_blocking`, which copes with bootstrap being called from
    inside an already-running event loop.
    """
    from pydantic import ValidationError

    from bos.core import ep_agent

    names = list(ep_agent.describe())
    if not names:
        return {}

    async def _gather() -> dict[str, Any]:
        return {name: await ep_agent.invoke(name) for name in names}

    raw = _run_coro_blocking(_gather())

    specs: dict[str, dict[str, Any]] = {}
    for name, result in raw.items():
        if not isinstance(result, dict):
            raise ValueError(f"ep_agent factory `{name}` must return an agent spec dict, got {type(result).__name__}")
        try:
            validated = AgentConfig.model_validate(result)
        except ValidationError as exc:
            raise ValueError(f"ep_agent factory `{name}` returned an invalid agent spec:\n{exc}") from exc
        specs[name] = _agent_config_to_core_kwargs(validated)
    return specs


def _resolve_agent_inheritance(specs: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Resolve ``_parent`` inheritance among ``[agents.<name>]`` specs (BEP 6 addendum).

    Each spec may name another ``[agents.*]`` agent via ``_parent``; the parent's
    resolved spec is deep-merged underneath the child (child keys win, per BEP 6
    merge semantics: dicts merge recursively, lists/scalars replace). Multi-level
    chains resolve transitively via memoized depth-first resolution. ``_parent`` is
    stripped from every returned spec. Cycles and unknown parents raise
    ``ValueError`` naming the offending chain / agent.
    """
    resolved: dict[str, dict[str, Any]] = {}

    def resolve(name: str, stack: tuple[str, ...]) -> dict[str, Any]:
        if name in resolved:
            return resolved[name]
        if name in stack:
            raise ValueError(f"Agent inheritance cycle via _parent: {' -> '.join([*stack, name])}")
        spec = dict(specs[name])
        parent = spec.pop("_parent", None)
        if parent is not None:
            if parent not in specs:
                known = ", ".join(sorted(specs)) or "none"
                raise ValueError(
                    f"Agent {name!r} _parent={parent!r} is not a defined [agents.*] agent. Known: {known}."
                )
            # Deep-copy the parent so merging the child cannot mutate the parent's
            # cached nested dicts (which would leak into siblings sharing the parent).
            spec = _deep_merge(copy.deepcopy(resolve(parent, (*stack, name))), spec)
        resolved[name] = spec
        return spec

    for name in specs:
        resolve(name, ())
    return resolved


class Workspace:
    def __init__(
        self,
        workspace: str | Path,
        bos_dir: str | Path,
        config: dict[str, Any] | RootConfig,
        *,
        config_file: str | Path | None = None,
    ):
        if isinstance(config, RootConfig):
            self.config: RootConfig = config
        elif isinstance(config, dict):
            try:
                self.config: RootConfig = validate_config(config)
            except Exception as exc:
                raise ConfigValidationError(str(exc)) from exc
        else:
            raise TypeError(f"config must be a dict or RootConfig, got {type(config).__name__}")
        self.workspace = _resolve_path(workspace)
        self.bos_dir = _resolve_path(bos_dir)
        self.config_file: Path | None = _resolve_path(config_file) if config_file else None
        self.agent_source_history: dict[str, list[AgentSourceRecord]] = {}

    @classmethod
    def from_discovery(cls, cwd: str | Path = ".") -> Workspace:
        """Discover workspace layout by walking ancestor directories.

        This is the legacy convenience constructor. Prefer explicit construction
        in new code.
        """
        resolved_cwd = _resolve_path(cwd)
        config_path = _resolve_config(resolved_cwd)
        bos_dir = config_path.parent
        config = _load_config(config_path)

        # Derive workspace root from config location
        if config_path.name == "config.toml" and config_path.parent.name == ".bos":
            workspace = config_path.parent.parent
        else:
            workspace = resolved_cwd

        return cls(workspace, bos_dir, config, config_file=config_path)

    def resolve_agents(self) -> None:
        """Load external agent files from agent_dirs into config.agents.

        Scans each directory in ``platform.agent_dirs`` for ``.toml`` and ``.md``
        files, validates each via :func:`validate_agent_config`, and stores them
        into ``self.config.agents``. External files with the same name as an
        inline agent replace it entirely (no merge).
        """
        agent_dirs = self.config.platform.agent_dirs if self.config.platform else ["./agents"]
        if not agent_dirs:
            return

        candidates: list[_LoadedAgentCandidate] = []
        load_order = 0
        for raw_dir in agent_dirs:
            agents_root = (self.bos_dir / Path(raw_dir.strip()).expanduser()).resolve()
            if not agents_root.exists() or not agents_root.is_dir():
                continue
            dir_candidates: list[Path] = []
            for entry in agents_root.iterdir():
                if entry.is_file() and entry.suffix.lower() in _EXTERNAL_AGENT_SUFFIXES:
                    dir_candidates.append(entry)
            for candidate_path in sorted(dir_candidates, key=lambda p: p.name):
                load_order += 1
                candidates.append(self._load_external_agent_candidate(candidate_path, load_order=load_order))

        if not candidates:
            return

        # External agent files replace inline agents with the same name.
        agents = self.config.agents or {}
        for candidate in candidates:
            name = candidate.spec["name"]
            validated = validate_agent_config(candidate.spec)
            agents[name] = AgentConfig.model_validate(validated)
        self.config.agents = agents

    def bootstrap_platform(self):
        """Bootstrap the platform from this Workspace.

        1. Load environment variables ([platform.envs] then [platform.envfile])
        2. Load extensions ([platform.extensions])
        3. Merge EP defaults from [exts] into registered EP implementations
        4. Register agents into AgentRegistry
        """
        # Importing this module registers the built-in BOS agent into ep_agent,
        # the same way bos.core.defaults registers the other built-ins — so it is
        # always available regardless of [platform.extensions], and resolves
        # through the normal factory path below rather than a bespoke fallback.
        import bos.config.bos_agent  # noqa: F401
        from bos.core import (
            AgentRegistry,
            _load_ext_modules,
            _load_ext_paths,
        )

        platform = self.config.platform
        bos_root = self.bos_dir

        # Use PlatformConfig defaults when no [platform] section is present
        envs = platform.envs if platform else {}
        envfile = platform.envfile if platform else None
        extensions = platform.extensions if platform else ["bos.exts", "./extensions"]

        # 1. Environment loading
        if envs:
            os.environ.update({k: str(v) for k, v in envs.items()})
        if envfile:
            from dotenv import load_dotenv

            load_dotenv((bos_root / Path(envfile).expanduser()).resolve(), override=True)

        # 2. Extension loading
        if extensions:
            modules, paths = [], []
            for ext in extensions:
                p = bos_root / Path(ext).expanduser()
                if p.exists():
                    paths.append(p)
                else:
                    modules.append(ext)
            if modules:
                _load_ext_modules(modules=modules)
            if paths:
                _load_ext_paths(paths=paths)

        # 3. Merge EP defaults from [exts]
        from bos.core import ExtensionPoint

        if self.config.exts:
            exts_data = self.config.exts.model_dump()
            # [exts.<key>] resolves through the ExtensionPoint name lookup, so
            # any registered name works (core EPs use `ep_*`, plugin-defined
            # ones `pep_*` by convention).
            for ep_key, impl_configs in exts_data.items():
                if not isinstance(impl_configs, dict):
                    continue
                ep = ExtensionPoint.lookup(ep_key)
                if ep is None:
                    logging.getLogger(__name__).warning(
                        f"[exts.{ep_key}] does not match any registered extension point; ignored"
                    )
                    continue
                for impl_name, cfg in impl_configs.items():
                    if isinstance(cfg, dict):
                        ep.update_defaults(impl_name, cfg)

        # 4. Agent registration
        # Resolution chain per agent name: [agent.defaults] -> ep_agent factory
        # result -> [agents.<name>] (with its _parent chain already folded in).
        # Factory and TOML terms may each be absent.
        agent_defaults: dict[str, Any] = {}
        if self.config.agent and self.config.agent.defaults:
            agent_defaults = _agent_config_to_core_kwargs(self.config.agent.defaults)

        factory_specs = _invoke_agent_factories()

        config_specs = {
            name: _agent_config_to_core_kwargs(agent_config)
            for name, agent_config in (self.config.agents or {}).items()
        }
        # Resolve [agents.<name>] _parent inheritance before the defaults/factory
        # merge, so each agent's effective TOML spec already folds in its base.
        config_specs = _resolve_agent_inheritance(config_specs)

        for name in {**factory_specs, **config_specs}:
            merged = _deep_merge(dict(agent_defaults), dict(factory_specs.get(name, {})))
            merged = _deep_merge(merged, config_specs.get(name, {}))
            merged.pop("name", None)  # name is the registration key, not a kwarg
            merged.pop("_parent", None)  # inheritance directive, not an Agent kwarg
            if "description" not in merged and name in factory_specs:
                from bos.core import ep_agent

                ext = ep_agent.get(name)
                if ext is not None and ext.description:
                    merged["description"] = ext.description
            AgentRegistry.register(name, **merged)

        # Suppress litellm auto-loading
        os.environ["LITELLM_MODE"] = "extension"
        logging.getLogger("LiteLLM").setLevel(logging.ERROR)

    def harness(self) -> AgentHarness:
        kwargs: dict[str, Any] = {}
        if self.config.harness:
            kwargs = self.config.harness.model_dump()
        return AgentHarness(
            bos_dir=self.bos_dir,
            workspace=self.workspace,
            consolidator=kwargs.get("consolidator", "LLMConsolidator"),
            chat_store=kwargs.get("chat_store", "JsonlChatStore"),
            mail_route=kwargs.get("mail_route", "JsonlMailRoute"),
            job_runner=kwargs.get("job_runner", "InProcJobRunner"),
            interceptors=kwargs.get("interceptors", []),
        )

    def get_main_agent_kind(self) -> str:
        runtime = self.config.runtime
        if not runtime or not runtime.actors:
            return DEFAULT_AGENT_KIND
        main_actor = runtime.main_actor
        actor = runtime.actors.get(main_actor)
        if actor is None:
            raise ValueError(f"runtime.main_actor {main_actor!r} must exist in runtime.actors.")
        return actor.agent

    def resolve_gateway_config(self) -> ResolvedGatewayConfig:
        from bos.gateway.config import ResolvedGatewayConfig

        gateway = self.config.runtime.gateway if self.config.runtime else None
        if gateway is None:
            return ResolvedGatewayConfig()
        return ResolvedGatewayConfig(
            host=gateway.host,
            port=gateway.port,
            upload_dir=gateway.upload_dir,
            max_upload_bytes=gateway.max_upload_bytes,
            api_key_env=gateway.api_key_env,
        )

    def resolve_gateway_actors(self) -> dict[str, ResolvedActorConfig]:
        from bos.gateway.config import ResolvedActorConfig

        runtime = self.config.runtime
        actors_cfg = runtime.actors if runtime else {}
        if not actors_cfg:
            raise ValueError("runtime.actors must define at least one actor for the gateway runtime.")

        actors: dict[str, ResolvedActorConfig] = {}
        for name, cfg in actors_cfg.items():
            self._validate_actor_name(name)
            agent_overrides = _agent_config_to_core_kwargs(cfg.agent_cfg)
            actors[name] = ResolvedActorConfig(
                name=name,
                agent=cfg.agent,
                address=f"agent@{name}",
                display_name=cfg.display_name,
                restart_on_error=cfg.restart_on_error,
                max_restarts=cfg.max_restarts,
                agent_overrides=agent_overrides,
            )
        return actors

    def resolve_default_actor(self) -> str:
        runtime = self.config.runtime
        main_actor = runtime.main_actor if runtime else "main"
        actors = self.resolve_gateway_actors()
        if main_actor not in actors:
            raise ValueError(f"runtime.main_actor {main_actor!r} must exist in runtime.actors.")
        return main_actor

    def resolve_gateway_channels(self) -> list[ResolvedGatewayChannelConfig]:
        from bos.gateway.config import ResolvedGatewayChannelConfig

        runtime = self.config.runtime
        raw_channels = runtime.channels if runtime else []
        actors = self.resolve_gateway_actors()
        default_actor = self.resolve_default_actor()
        seen_channel_ids: set[str] = set()
        channels: list[ResolvedGatewayChannelConfig] = []

        for idx, cfg in enumerate(raw_channels, start=1):
            channel_type = cfg.type.strip()
            if not channel_type:
                raise ValueError(f"Channel entry #{idx} must define type.")
            if channel_type == "HttpChannel":
                raise ValueError("HttpChannel is gateway infrastructure and cannot be configured as a channel.")
            channel_id = cfg.channel_id.strip()
            if not channel_id:
                raise ValueError(f"Channel {channel_type!r} must define channel_id.")
            if channel_id in seen_channel_ids:
                raise ValueError(f"Duplicate channel_id: {channel_id!r}")
            seen_channel_ids.add(channel_id)

            target_actor = (cfg.target_actor or default_actor).strip()
            if target_actor not in actors:
                raise ValueError(f"Channel {channel_id!r} target_actor {target_actor!r} must exist in runtime.actors.")
            settings = cfg.settings or {}
            if not isinstance(settings, dict):
                raise ValueError(f"Channel {channel_id!r} settings must be a table/dict.")
            channels.append(
                ResolvedGatewayChannelConfig(
                    type=channel_type,
                    channel_id=channel_id,
                    address=f"channel@{channel_id}",
                    target_actor=target_actor,
                    display_name=str(cfg.display_name) if cfg.display_name is not None else None,
                    settings=dict(settings),
                )
            )
        return channels

    def resolve_gateway_runtime(self) -> GatewayRuntimeConfig:
        """Assemble the single gateway-owned aggregate the gateway is built from.

        The gateway never reads a ``Workspace``; the composition root calls this
        and injects the result via ``Gateway(runtime=...)`` (BEP 13 §3.3)."""
        from bos.gateway.config import GatewayRuntimeConfig

        runtime = self.config.runtime
        mention_prefix = runtime.actor_resolver.mention_prefix if runtime else "@"
        return GatewayRuntimeConfig(
            gateway=self.resolve_gateway_config(),
            actors=self.resolve_gateway_actors(),
            default_actor=self.resolve_default_actor(),
            channels=self.resolve_gateway_channels(),
            mention_prefix=mention_prefix,
            workdir=str(self.workspace),
            bos_dir=self.bos_dir,
        )

    def resolve_platform_envfile(self) -> Path | None:
        platform = self.config.platform
        if platform is None or not platform.envfile:
            return None
        return (self.bos_dir / Path(platform.envfile).expanduser()).resolve()

    @staticmethod
    def _validate_actor_name(name: str) -> None:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", name):
            raise ValueError(f"Invalid actor name {name!r}; expected a mention-safe actor identity.")

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

            # Resolve relative entries against .bos/; absolute entries remain absolute.
            agents_root = (self.bos_dir / Path(raw_dir.strip()).expanduser()).resolve()

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

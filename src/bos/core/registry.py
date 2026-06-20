from __future__ import annotations

import inspect
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, ClassVar

from ._utils import _apply, _apply_async, _compact, _deep_merge

logger = logging.getLogger("bos")


@dataclass
class Extension:
    name: str
    fn: Callable[..., Any]
    description: str = ""
    defaults: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


class ExtensionPoint:
    """Named registry of interchangeable implementations.

    Every extension point requires a unique ``name``. Public names are
    recorded in a class-level lookup (see :meth:`lookup`) and resolve
    ``[exts.<name>]`` config sections; a duplicate public name raises at
    construction time, crashing startup. Names with a leading underscore
    are private: not configurable, kept out of the lookup, and may be
    instantiated repeatedly (e.g. per-agent local tool registries).

    Naming convention (not enforced): core extension points defined in
    ``bos.core.contract`` are named ``ep_<name>``; extension points defined
    by plugins are named ``pep_<name>`` (plugin extension point) so the two
    are distinguishable at a glance and in ``[exts]`` config keys.
    """

    _by_name: ClassVar[dict[str, "ExtensionPoint"]] = {}

    def __init__(
        self,
        name: str,
        description: str = "",
        validate: Callable[..., bool] | None = None,
    ) -> None:
        if not name or not isinstance(name, str):
            raise ValueError("ExtensionPoint requires a non-empty name")
        self.name = name
        self.description = description
        self._validate = validate or getattr(self, "default_validate", None)
        self._extensions: dict[str, Extension] = {}
        self.get = self._extensions.get
        self.has = lambda name: name in self._extensions
        self.describe = lambda: {k: v.description for k, v in self._extensions.items()}
        if not name.startswith("_"):
            if name in ExtensionPoint._by_name:
                raise ValueError(f"Duplicate extension point name `{name}`; extension point names must be unique")
            ExtensionPoint._by_name[name] = self

    @classmethod
    def lookup(cls, name: str) -> "ExtensionPoint | None":
        """Return the extension point registered under *name*, if any."""
        return ExtensionPoint._by_name.get(name)

    def register(self, ext: Extension) -> None:
        if ext.name in self._extensions:
            logger.warning(
                f"Set default provider for extension point: {self.description}"
                if ext.name == "_default"
                else f"Extension `{ext.name}` got overwritten for extension point: {self.description}"
            )
        self._extensions[ext.name] = ext

    async def invoke(self, name: str, kwargs: dict[str, Any] | None = None) -> Any:
        """Invoke extension *name*, awaiting its result when the fn is async.

        Implementations may be sync or async functions interchangeably;
        callers always await.
        """
        ext = self.get(name)
        if ext is None:
            raise ValueError(f"Extension '{name}' not found for '{self.description[:30].strip()}...'")
        return await _apply_async(ext.fn, _compact(ext.defaults, kwargs or {}))

    def update_defaults(self, name: str, defaults: dict[str, Any]) -> None:
        """Merge *defaults* into the registered defaults for extension *name*.

        If *name* is not registered yet, the call is a no-op (the EP may be
        registered later during extension loading).
        """
        ext = self._extensions.get(name)
        if ext is None or not defaults:
            return
        try:
            origin = _deep_merge({}, ext.defaults or {})
            _deep_merge(origin, defaults)
            ext.defaults = origin
        except Exception:
            logger.warning(f"Failed to update defaults for extension {name}", exc_info=True)

    def __call__(
        self,
        *,
        name: str | None = None,
        description: str | None = None,
        defaults: dict[str, Any] | Callable[[], dict[str, Any]] | None = None,
        **metadata: Any,
    ) -> Callable[[Callable[..., Any]], Any]:
        def decorator(fn: Any) -> Any:
            ext_name = name or getattr(fn, "__name__", None)
            if ext_name is None:
                raise ValueError("Extension name is required")
            ext = Extension(
                name=ext_name,
                description=description or getattr(fn, "__doc__", ""),
                defaults=defaults() if callable(defaults) else (defaults or {}),
                metadata=metadata,
                fn=fn,
            )
            if self._validate and not _apply(self._validate, {"fn": fn, "ext": ext, "ext_point": self}):
                raise ValueError(f"Extension is not valid:\n{description}")
            self.register(ext)
            return fn

        return decorator


class ToolRegistry(ExtensionPoint):
    _JSON_TYPES = (dict, list, tuple, int, float, bool, type(None))

    def to_openai_schema(self) -> dict[str, dict[str, Any]]:
        return {t.name: self.build_openai_schema(t) for t in self._extensions.values()}

    async def invoke(self, name: str, kwargs: dict[str, Any] | None = None) -> str:
        if ext := self.get(name):
            result = await _apply_async(ext.fn, _compact(ext.defaults, kwargs or {}))
            return self._serialize_result(ext, result)
        raise ValueError(f"Extension '{name}' not found for '{self.description[:30].strip()}...'")

    @staticmethod
    def build_openai_schema(ext: Extension) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": ext.name,
                "description": ext.description,
                "parameters": ext.metadata["parameters"],
            },
        }

    def _serialize_result(self, ext: Extension, result: Any) -> str:
        serializer = ext.metadata.get("result_serializer", "auto")
        if serializer == "json":
            return json.dumps(result, default=str)
        if serializer == "str":
            return str(result)
        if serializer != "auto":
            raise ValueError(f"Tool {ext.name} has unsupported result_serializer {serializer!r}")
        if isinstance(result, self._JSON_TYPES):
            return json.dumps(result, default=str)
        return str(result)

    def default_validate(self, ext: Extension) -> bool:
        if "parameters" not in ext.metadata:
            raise ValueError(f"Tool {ext.name} is missing parameters")
        signature = inspect.signature(ext.fn)
        fn_params = set(signature.parameters.keys())
        meta_params = set(ext.metadata["parameters"]["properties"].keys())
        has_varkw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values())
        if not has_varkw and not meta_params.issubset(fn_params):
            raise ValueError(f"Tool {ext.name} schema parameters must be a subset of the function signature")
        serializer = ext.metadata.get("result_serializer", "auto")
        if serializer not in {"auto", "json", "str"}:
            raise ValueError(f"Tool {ext.name} has unsupported result_serializer {serializer!r}")
        if not ext.description:
            logger.warning(f"Tool {ext.name} is missing description")
        return True

    def describe_usage(self) -> dict[str, str]:
        return {k: v.metadata.get("usage", v.description) for k, v in self._extensions.items()}

    def metadata_for(self, name: str) -> dict[str, Any]:
        ext = self.get(name)
        return ext.metadata if ext is not None else {}

from __future__ import annotations

import inspect
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from ._utils import _apply, _apply_async, _compact

logger = logging.getLogger("bos")


@dataclass
class Extension:
    name: str
    fn: Callable[..., Any]
    description: str = ""
    defaults: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


class ExtensionPoint:
    def __init__(
        self,
        description: str,
        validate: Callable[..., bool] | None = None,
    ) -> None:
        self.description = description
        self._validate = validate or getattr(self, "default_validate", None)
        self._extensions: dict[str, Extension] = {}
        self.get = self._extensions.get
        self.has = lambda name: name in self._extensions
        self.describe = lambda: {k: v.description for k, v in self._extensions.items()}

    def register(self, ext: Extension) -> None:
        if ext.name in self._extensions:
            logger.warning(
                f"Set default provider for extension point: {self.description}"
                if ext.name == "_default"
                else f"Extension `{ext.name}` got overwritten for extension point: {self.description}"
            )
        self._extensions[ext.name] = ext

    def invoke(self, name: str, kwargs: dict[str, Any] | None = None) -> Any:
        if name not in self._extensions:
            raise ValueError(f"Extension '{name}' not found for '{self.description[:30].strip()}...'")
        return _apply(self.get(name).fn, _compact(self.get(name).defaults, kwargs or {}))

    async def invoke_async(self, name: str, kwargs: dict[str, Any] | None = None) -> Any:
        return await _apply_async(self.get(name).fn, _compact(self.get(name).defaults, kwargs or {}))

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
                defaults=defaults,
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

    def invoke(self, name: str, kwargs: dict[str, Any] | None = None) -> str:
        if name not in self._extensions:
            raise ValueError(f"Extension '{name}' not found for '{self.description[:30].strip()}...'")
        ext = self.get(name)
        result = _apply(ext.fn, _compact(ext.defaults, kwargs or {}))
        return self._serialize_result(ext, result)

    async def invoke_async(self, name: str, kwargs: dict[str, Any] | None = None) -> str:
        if name not in self._extensions:
            raise ValueError(f"Extension '{name}' not found for '{self.description[:30].strip()}...'")
        ext = self.get(name)
        result = await _apply_async(ext.fn, _compact(ext.defaults, kwargs or {}))
        return self._serialize_result(ext, result)

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

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger("bos")


def _compact(*dicts: dict, **kwargs: Any) -> dict[str, Any]:
    merged = {}
    [merged.update(d) for d in (*dicts, kwargs) if d is not None]
    return {k: v for k, v in merged.items() if v is not None}


def _build_params(fn: Callable, params: dict[str, Any]) -> tuple[list[Any], dict[str, Any]]:
    sig = inspect.signature(fn)
    has_varkw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
    valid_params = params if has_varkw else {k: v for k, v in params.items() if k in sig.parameters}
    bound = sig.bind_partial(**valid_params)
    bound.apply_defaults()
    return bound.args, bound.kwargs


def _apply(fn: Callable, params: dict[str, Any]) -> Any:
    args, kwargs = _build_params(fn, params)
    return fn(*args, **kwargs)


async def _apply_async(fn: Callable, params: dict[str, Any]) -> Any:
    args, kwargs = _build_params(fn, params)
    result = fn(*args, **kwargs)
    return await result if inspect.isawaitable(result) else result


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
    def to_openai_schema(self) -> dict[str, dict[str, Any]]:
        return {t.name: self.build_openai_schema(t) for t in self._extensions.values()}

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

    def default_validate(self, ext: Extension) -> bool:
        if "parameters" not in ext.metadata:
            raise ValueError(f"Tool {ext.name} is missing parameters")
        fn_params = set(inspect.signature(ext.fn).parameters.keys())
        meta_params = set(ext.metadata["parameters"]["properties"].keys())
        if fn_params != meta_params:
            raise ValueError(f"Tool {ext.name} parameters do not match the function signature")
        if not ext.description:
            logger.warning(f"Tool {ext.name} is missing description")
        return True

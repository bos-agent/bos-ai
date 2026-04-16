from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from bos.core.registry import ExtensionPoint

if TYPE_CHECKING:
    from bos.core.agent import ReactContext


ep_react_interceptor = ExtensionPoint(
    description="React Interceptor. A factory that creates interceptors implementing the ReactInterceptor protocol."
)


@runtime_checkable
class ReactInterceptor(Protocol):
    async def intercept(
        self,
        stage: Literal[
            "prepare",
            "before_llm",
            "after_llm",
            "after_tool",
            "final_response",
            "max_iteration",
        ],
        context: ReactContext,
    ) -> None: ...


class ChainReactInterceptor:
    """
    An interceptor that takes a list of interceptor names (or configurations)
    and runs them sequentially in the provided order.
    """

    def __init__(self, interceptors: list[str | dict[str, Any]] | None = None) -> None:
        self._configs = [
            cfg.copy() if isinstance(cfg, dict) else {"name": cfg}
            for cfg in (interceptors or [])
            if isinstance(cfg, str) or (isinstance(cfg, dict) and "name" in cfg)
        ]
        self._instances: list[ReactInterceptor] = [None] * len(self._configs)

    async def aclose(self) -> None:
        from bos.core import _aclose

        for interceptor in self._instances:
            await _aclose(interceptor)

    async def intercept(
        self,
        stage: Literal[
            "prepare",
            "before_llm",
            "after_llm",
            "after_tool",
            "final_response",
            "max_iteration",
        ],
        context: ReactContext,
    ) -> None:
        from bos.core import _create_extension_instance, logger

        for i, cfg in enumerate(self._configs):
            if self._instances[i] is None and ep_react_interceptor.has(cfg["name"]):
                try:
                    self._instances[i] = _create_extension_instance(ep_react_interceptor, ReactInterceptor, cfg)
                except Exception as e:
                    self._instances[i] = e
                    logger.error(f"Failed to create interceptor {cfg['name']}: {e}")
            if isinstance(self._instances[i], ReactInterceptor):
                await self._instances[i].intercept(stage, context)

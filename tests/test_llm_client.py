"""The built-in litellm provider names its extra when litellm is absent (BEP 16 §5.3)."""

import sys

import pytest
from conftest import BlockImport

from bos.core.agent import LLMResponse
from bos.core.contract import ep_provider
from bos.core.llm import LLMClient


class TestBuiltinProviderWithoutLitellm:
    @pytest.mark.asyncio
    async def test_names_the_extra_and_the_hook(self, monkeypatch):
        # The provider registers at import time regardless of whether litellm is
        # installed, so `ep_provider.has("litellm")` is True on a base install —
        # the failure can only surface from inside the call.
        import bos.core.defaults  # noqa: F401

        assert ep_provider.has("litellm")

        monkeypatch.setattr(sys, "meta_path", [BlockImport("litellm"), *sys.meta_path])
        monkeypatch.delitem(sys.modules, "litellm", raising=False)
        monkeypatch.delenv("BOS_MODEL", raising=False)

        with pytest.raises(ValueError) as excinfo:
            await LLMClient().complete([{"role": "user", "content": "hi"}], model="gpt-4o")

        message = str(excinfo.value)
        assert "bos-ai[litellm]" in message
        assert "@ep_provider" in message

    @pytest.mark.asyncio
    async def test_unknown_model_prefix_falls_back_to_the_builtin_provider(self, monkeypatch):
        import bos.core.defaults  # noqa: F401

        monkeypatch.setattr(sys, "meta_path", [BlockImport("litellm"), *sys.meta_path])
        monkeypatch.delitem(sys.modules, "litellm", raising=False)
        monkeypatch.delenv("BOS_MODEL", raising=False)

        # An unregistered prefix is not an error — `complete` treats the whole
        # string as a litellm model name, so the litellm guard is what reports.
        with pytest.raises(ValueError) as excinfo:
            await LLMClient().complete([{"role": "user", "content": "hi"}], model="nosuch/model")

        assert "bos-ai[litellm]" in str(excinfo.value)


class TestProviderRouting:
    @pytest.mark.asyncio
    async def test_registered_provider_is_invoked_with_the_bare_model_name(self, monkeypatch):
        monkeypatch.delenv("BOS_MODEL", raising=False)
        seen: dict[str, object] = {}

        @ep_provider(name="_test_echo")
        async def _echo(messages: list[dict], model: str, **kwargs: object) -> LLMResponse:
            seen["model"] = model
            return LLMResponse(content="ok")

        try:
            response = await LLMClient().complete([{"role": "user", "content": "hi"}], model="_test_echo/demo")
        finally:
            ep_provider._extensions.pop("_test_echo", None)

        assert response.content == "ok"
        assert seen["model"] == "demo"

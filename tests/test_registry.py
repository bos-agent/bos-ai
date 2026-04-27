import pytest

from bos.core.registry import ToolRegistry


def test_tool_registry_allows_runtime_injected_function_arguments():
    registry = ToolRegistry("test tools")

    @registry(
        name="EchoWithContext",
        description="Echo input with runtime context.",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    )
    def tool_echo_with_context(text: str, chat_id: str, turn_id: str) -> dict[str, str]:
        return {"text": text, "chat_id": chat_id, "turn_id": turn_id}

    result = registry.invoke(
        "EchoWithContext",
        {"text": "hello", "chat_id": "chat-1", "turn_id": "turn-1"},
    )

    assert result == '{"text": "hello", "chat_id": "chat-1", "turn_id": "turn-1"}'


@pytest.mark.asyncio
async def test_tool_registry_invoke_async_auto_serializes_json_results():
    registry = ToolRegistry("test tools")

    @registry(
        name="BuildList",
        description="Return a list payload.",
        parameters={
            "type": "object",
            "properties": {"count": {"type": "integer"}},
            "required": ["count"],
        },
    )
    async def tool_build_list(count: int) -> list[int]:
        return list(range(count))

    result = await registry.invoke_async("BuildList", {"count": 3})

    assert result == "[0, 1, 2]"


def test_tool_registry_can_force_result_serialization_mode():
    registry = ToolRegistry("test tools")

    @registry(
        name="ForceJson",
        description="Return a JSON scalar.",
        parameters={"type": "object", "properties": {}, "required": []},
        result_serializer="json",
    )
    def tool_force_json() -> bool:
        return True

    @registry(
        name="ForceString",
        description="Return a stringified dict.",
        parameters={"type": "object", "properties": {}, "required": []},
        result_serializer="str",
    )
    def tool_force_string() -> dict[str, int]:
        return {"value": 1}

    assert registry.invoke("ForceJson", {}) == "true"
    assert registry.invoke("ForceString", {}) == "{'value': 1}"


def test_tool_registry_rejects_unsupported_result_serializer():
    registry = ToolRegistry("test tools")

    with pytest.raises(ValueError, match="unsupported result_serializer"):

        @registry(
            name="BadSerializer",
            description="Bad serializer.",
            parameters={"type": "object", "properties": {}, "required": []},
            result_serializer="yaml",
        )
        def tool_bad_serializer() -> str:
            return "bad"

import pytest

from bos.core.registry import ExtensionPoint, ToolRegistry


def test_extension_point_requires_a_name():
    with pytest.raises(ValueError, match="non-empty name"):
        ExtensionPoint("")


def test_extension_point_public_names_are_unique():
    point = ExtensionPoint("unique_point_test", "first")
    assert ExtensionPoint.lookup("unique_point_test") is point
    with pytest.raises(ValueError, match="Duplicate extension point name"):
        ExtensionPoint("unique_point_test", "second")


def test_extension_point_private_names_skip_the_lookup():
    ExtensionPoint("_private_point", "first")
    ExtensionPoint("_private_point", "second")  # repeated instantiation is allowed
    assert ExtensionPoint.lookup("_private_point") is None


def test_tool_registry_allows_runtime_injected_function_arguments():
    registry = ToolRegistry("_test_tools")

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
    registry = ToolRegistry("_test_tools")

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
    registry = ToolRegistry("_test_tools")

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


def test_describe_usage_returns_usage_when_provided():
    registry = ToolRegistry("_test_tools")

    @registry(
        name="MyTool",
        description="Does something.",
        usage="Does something with extra guidance for the prompt.",
        parameters={"type": "object", "properties": {}, "required": []},
    )
    def tool_my() -> str:
        return "ok"

    assert registry.describe_usage() == {"MyTool": "Does something with extra guidance for the prompt."}


def test_describe_usage_falls_back_to_description():
    registry = ToolRegistry("_test_tools")

    @registry(
        name="MyTool",
        description="Does something.",
        parameters={"type": "object", "properties": {}, "required": []},
    )
    def tool_my() -> str:
        return "ok"

    assert registry.describe_usage() == {"MyTool": "Does something."}


def test_build_openai_schema_uses_description_not_usage():
    registry = ToolRegistry("_test_tools")

    @registry(
        name="MyTool",
        description="API description.",
        usage="Prompt-facing usage guidance.",
        parameters={
            "type": "object",
            "properties": {"x": {"type": "integer"}},
            "required": ["x"],
        },
    )
    def tool_my(x: int) -> str:
        return str(x)

    schema = registry.to_openai_schema()
    assert schema["MyTool"]["function"]["description"] == "API description."


def test_describe_still_returns_description():
    registry = ToolRegistry("_test_tools")

    @registry(
        name="MyTool",
        description="API description.",
        usage="Prompt-facing usage guidance.",
        parameters={"type": "object", "properties": {}, "required": []},
    )
    def tool_my() -> str:
        return "ok"

    assert registry.describe() == {"MyTool": "API description."}


def test_tool_registry_rejects_unsupported_result_serializer():
    registry = ToolRegistry("_test_tools")

    with pytest.raises(ValueError, match="unsupported result_serializer"):

        @registry(
            name="BadSerializer",
            description="Bad serializer.",
            parameters={"type": "object", "properties": {}, "required": []},
            result_serializer="yaml",
        )
        def tool_bad_serializer() -> str:
            return "bad"

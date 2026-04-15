import logging

from bos.harness import AgentHarness


def test_harness_local_tools_describe_ask_subagent(caplog):
    harness = AgentHarness()

    with caplog.at_level(logging.WARNING):
        tools = harness._create_local_tools()

    ask_subagent = tools.get("AskSubagent")
    assert ask_subagent.description == "Delegate a task to a named subagent and return its response."
    assert not any("Tool AskSubagent is missing description" in record.message for record in caplog.records)

    schema = tools.to_openai_schema()["AskSubagent"]
    assert schema["function"]["description"] == ask_subagent.description

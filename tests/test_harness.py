import logging

import pytest

from bos.core import AgentHarness, Mailbox, ep_mailbox


def test_harness_local_tools_describe_ask_subagent(caplog):
    harness = AgentHarness()

    with caplog.at_level(logging.WARNING):
        tools = harness._create_local_tools()

    ask_subagent = tools.get("AskSubagent")
    assert ask_subagent.description == "Delegate a task to a named subagent and return its response."
    assert not any("Tool AskSubagent is missing description" in record.message for record in caplog.records)

    schema = tools.to_openai_schema()["AskSubagent"]
    assert schema["function"]["description"] == ask_subagent.description


def test_mailbox_backcompat_aliases_point_to_mail_route_surface():
    assert Mailbox is not None
    assert ep_mailbox is not None


@pytest.mark.asyncio
async def test_harness_accepts_legacy_mailbox_config_and_send_mail_falls_back_to_agent_address(tmp_path):
    bos_dir = tmp_path / ".bos"
    bos_dir.mkdir()

    async with AgentHarness(mailbox={"name": "JsonlMailRoute", "store_dir": tmp_path}, bos_dir=bos_dir) as harness:
        assert harness.mailbox is harness.mail_route

        receiver = harness.mail_route.bind("bob")
        await receiver.receive_nowait()

        agent = harness.create_agent()
        result = await agent._local_tools.invoke_async("SendMail", {"recipient": "bob", "content": "hello"})

        assert result == "(Sent to bob)"

        message = await receiver.receive_nowait()
        assert message is not None
        assert message.sender == "agent@_default"
        assert message.content == "hello"

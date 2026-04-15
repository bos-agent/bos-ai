from __future__ import annotations

from enum import StrEnum


class MessageType(StrEnum):
    MESSAGE = "message"
    COMMAND = "command"
    COMMAND_RESULT = "command_result"
    CHANNEL_COMMAND = "channel_command"
    SYSTEM = "system"
    ECHO = "echo"
    AGENT_STEP = "agent_step"
    INTERRUPT_MESSAGE = "interrupt_message"
    INTERRUPT_ABORT = "interrupt_abort"

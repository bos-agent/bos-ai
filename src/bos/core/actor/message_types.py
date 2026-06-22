from __future__ import annotations

from enum import StrEnum


class MessageType(StrEnum):
    MESSAGE = "message"
    COMMAND = "command"
    COMMAND_RESULT = "command_result"
    SYSTEM = "system"
    ECHO = "echo"
    TURN_EVENT = "turn_event"
    INTERRUPT_MESSAGE = "interrupt_message"
    INTERRUPT_ABORT = "interrupt_abort"

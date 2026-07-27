"""Minimal Hermes gateway doubles used to load the platform plugin in isolation."""

from __future__ import annotations

import enum
import sys
import types
from dataclasses import dataclass


gateway = types.ModuleType("gateway")
gateway_config = types.ModuleType("gateway.config")
gateway_platforms = types.ModuleType("gateway.platforms")
gateway_base = types.ModuleType("gateway.platforms.base")


class Platform(str):
    pass


class MessageType(enum.Enum):
    TEXT = "text"


@dataclass
class MessageEvent:
    text: str
    message_type: MessageType
    source: object
    raw_message: bytes
    message_id: str
    timestamp: object
    metadata: dict


@dataclass
class SendResult:
    success: bool
    error: str | None = None
    retryable: bool = False
    message_id: str | None = None
    raw_response: bytes | None = None


class BasePlatformAdapter:
    def __init__(self, config, platform):
        self.config = config
        self.platform = platform
        self.connected = False
        self.fatal_error = None
        self.handled_messages = []

    def _set_fatal_error(self, code, message, retryable):
        self.fatal_error = (code, message, retryable)

    def _mark_connected(self):
        self.connected = True

    def _mark_disconnected(self):
        self.connected = False

    def build_source(self, **values):
        return values

    async def handle_message(self, event):
        self.handled_messages.append(event)


gateway_config.Platform = Platform
gateway_base.BasePlatformAdapter = BasePlatformAdapter
gateway_base.MessageEvent = MessageEvent
gateway_base.MessageType = MessageType
gateway_base.SendResult = SendResult

sys.modules.setdefault("gateway", gateway)
sys.modules.setdefault("gateway.config", gateway_config)
sys.modules.setdefault("gateway.platforms", gateway_platforms)
sys.modules.setdefault("gateway.platforms.base", gateway_base)

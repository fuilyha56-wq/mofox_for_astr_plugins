"""AstrBot 兼容 SDK 的事件结果与消息链。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .message_components import (
    BaseMessageComponent,
    Image,
    Plain,
    chain_to_onebot_segments,
    chain_to_text,
)


class EventResultType:
    """结果类型枚举。"""

    PLAIN = "plain"
    IMAGE = "image"
    CHAIN = "chain"
    COMMAND = "command"


class ResultContentType:
    """命令结果内容类型。"""

    TEXT = "text"
    IMAGE = "image"
    MIXED = "mixed"


@dataclass
class MessageEventResult:
    """AstrBot 处理器返回的消息事件结果。"""

    type: str = EventResultType.PLAIN
    chain: list[BaseMessageComponent] = field(default_factory=list)
    result_content_type: str = ResultContentType.TEXT
    is_stopped: bool = False

    @classmethod
    def plain(cls, text: str) -> "MessageEventResult":
        """构造纯文本结果。"""
        return cls(type=EventResultType.PLAIN, chain=[Plain(text=str(text))])

    @classmethod
    def image(cls, url: str) -> "MessageEventResult":
        """构造图片结果。"""
        return cls(type=EventResultType.IMAGE, chain=[Image.fromURL(url)])

    @classmethod
    def chain_result(cls, chain: Iterable[BaseMessageComponent]) -> "MessageEventResult":
        """构造消息链结果。"""
        return cls(type=EventResultType.CHAIN, chain=list(chain))

    @property
    def text(self) -> str:
        """返回结果中的纯文本。"""
        return chain_to_text(self.chain)

    def to_segments(self) -> list[dict[str, Any]]:
        """转换完整链为严格 OneBot 原始段。"""
        return chain_to_onebot_segments(self.chain)


@dataclass
class CommandResult:
    """命令结果对象。"""

    success: bool = True
    message: str = ""
    content_type: str = ResultContentType.TEXT


class MessageChain:
    """AstrBot 主动消息链构造器。"""

    def __init__(self, chain: Iterable[BaseMessageComponent] | None = None) -> None:
        """以可选既有组件链初始化消息链。"""
        self._chain: list[BaseMessageComponent] = list(chain or [])

    def message(self, text: str) -> "MessageChain":
        """追加文本。"""
        self._chain.append(Plain(text=str(text)))
        return self

    def text(self, text: str) -> "MessageChain":
        """追加文本（message 别名）。"""
        return self.message(text)

    def image(self, url_or_path: str) -> "MessageChain":
        """追加图片。"""
        self._chain.append(Image.fromURL(url_or_path))
        return self

    def file_image(self, path: str) -> "MessageChain":
        """追加本地图片。"""
        self._chain.append(Image.fromFileSystem(path))
        return self

    def at(self, qq: str | int) -> "MessageChain":
        """追加 @。"""
        from .message_components import At

        self._chain.append(At(qq=qq))
        return self

    @property
    def chain(self) -> list[BaseMessageComponent]:
        """返回内部消息链的快照。"""
        return list(self._chain)

    @chain.setter
    def chain(self, value: Iterable[BaseMessageComponent]) -> None:
        """替换消息链内容。"""
        self._chain = list(value)

    def to_segments(self) -> list[dict[str, Any]]:
        """转换完整链为严格 OneBot 原始段。"""
        return chain_to_onebot_segments(self._chain)

    def to_text(self) -> str:
        """转换为纯文本。"""
        return chain_to_text(self._chain)

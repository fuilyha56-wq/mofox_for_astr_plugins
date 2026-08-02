"""AstrBot 消息组件及其严格的 OneBot 段转换。

本模块只转换可被 ``onebot_expand.MessageService`` 无损发送的组件。未知或
没有有效来源的组件会抛出明确错误，而不是被拼成文本占位符。
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from ..errors import UnsupportedMessageSegmentError


@dataclass
class BaseMessageComponent:
    """AstrBot 消息组件基类。"""

    type: ClassVar[str] = ""

    def to_dict(self) -> dict[str, Any]:
        """以 AstrBot 风格序列化当前组件。"""
        return {"type": self.type, "data": {}}


@dataclass
class Plain(BaseMessageComponent):
    """纯文本消息组件。"""

    text: str = ""
    type: ClassVar[str] = "plain"

    def to_dict(self) -> dict[str, Any]:
        """返回文本组件的序列化表示。"""
        return {"type": "plain", "data": {"text": self.text}}


@dataclass
class At(BaseMessageComponent):
    """OneBot @ 用户消息组件。"""

    qq: str | int = ""
    name: str = ""
    type: ClassVar[str] = "at"


@dataclass
class Image(BaseMessageComponent):
    """图片消息组件，支持 URL、文件路径和 Base64。"""

    file: str = ""
    url: str = ""
    type: ClassVar[str] = "image"

    @classmethod
    def fromURL(cls, url: str) -> "Image":
        """从 HTTP(S) URL 构造图片组件。"""
        return cls(file=str(url), url=str(url))

    @classmethod
    def fromFileSystem(cls, path: str) -> "Image":
        """从本地文件路径构造图片组件。"""
        normalized = str(path)
        return cls(file=f"file:///{normalized}", url=normalized)

    @classmethod
    def fromBase64(cls, data: str) -> "Image":
        """从不带或带 ``base64://`` 前缀的 Base64 数据构造图片组件。"""
        normalized = str(data).strip()
        if not normalized:
            raise ValueError("图片 Base64 数据不能为空")
        if not normalized.startswith("base64://"):
            normalized = f"base64://{normalized}"
        return cls(file=normalized)

    @classmethod
    def fromBytes(cls, data: bytes) -> "Image":
        """从二进制图片内容构造 Base64 图片组件。"""
        if not isinstance(data, bytes) or not data:
            raise ValueError("图片字节数据必须为非空 bytes")
        return cls.fromBase64(base64.b64encode(data).decode("ascii"))


@dataclass
class Face(BaseMessageComponent):
    """QQ 表情组件；当前 OneBot 出站链路不支持无损发送。"""

    id: int = 0
    type: ClassVar[str] = "face"


@dataclass
class Record(BaseMessageComponent):
    """语音消息组件。"""

    file: str = ""
    url: str = ""
    type: ClassVar[str] = "record"


@dataclass
class Video(BaseMessageComponent):
    """视频消息组件。"""

    file: str = ""
    url: str = ""
    type: ClassVar[str] = "video"

    @classmethod
    def fromFileSystem(cls, path: str) -> "Video":
        """从本地文件路径构造视频组件。"""
        normalized = str(path)
        return cls(file=f"file:///{normalized}", url=normalized)

    @classmethod
    def fromURL(cls, url: str) -> "Video":
        """从 URL 构造视频组件。"""
        return cls(file=str(url), url=str(url))


@dataclass
class File(BaseMessageComponent):
    """文件消息组件。"""

    file: str = ""
    name: str = ""
    type: ClassVar[str] = "file"


@dataclass
class Reply(BaseMessageComponent):
    """引用回复组件。"""

    id: str = ""
    type: ClassVar[str] = "reply"


@dataclass
class Node:
    """合并转发节点定义；当前不可无损映射。"""

    uin: int | str = 0
    name: str = ""
    content: list[BaseMessageComponent] = field(default_factory=list)


@dataclass
class Nodes(BaseMessageComponent):
    """合并转发节点列表；当前不可无损映射。"""

    nodes: list[Node] = field(default_factory=list)
    type: ClassVar[str] = "nodes"


@dataclass
class Poke(BaseMessageComponent):
    """戳一戳组件；当前不属于桥的消息链支持范围。"""

    name: str = ""
    qq: str | int = ""
    type: ClassVar[str] = "poke"


@dataclass
class Music(BaseMessageComponent):
    """音乐组件；当前不属于桥的消息链支持范围。"""

    title: str = ""
    url: str = ""
    type: ClassVar[str] = "music"


@dataclass
class Forward(BaseMessageComponent):
    """转发消息组件；当前不属于桥的消息链支持范围。"""

    id: str = ""
    type: ClassVar[str] = "forward"


ComponentTypes: dict[str, type[Any]] = {
    "plain": Plain,
    "text": Plain,
    "face": Face,
    "record": Record,
    "video": Video,
    "at": At,
    "music": Music,
    "image": Image,
    "reply": Reply,
    "forward": Forward,
    "node": Node,
    "nodes": Nodes,
    "poke": Poke,
    "file": File,
}

_COMPONENT_CLASS_NAMES = frozenset(
    {"Plain", "At", "Image", "Record", "Reply", "Video", "File"}
)
_COMPONENT_KIND_BY_CLASS_NAME = {
    "Plain": "plain",
    "At": "at",
    "Image": "image",
    "Record": "record",
    "Reply": "reply",
    "Video": "video",
    "File": "file",
}
_COMPONENT_TYPE_VALUES = {
    "Plain": frozenset({"plain", "text"}),
    "At": frozenset({"at"}),
    "Image": frozenset({"image"}),
    "Record": frozenset({"record", "voice"}),
    "Reply": frozenset({"reply"}),
    "Video": frozenset({"video"}),
    "File": frozenset({"file"}),
}
_compatible_component_type_counts: dict[str, dict[type[Any], int]] = {
    name: {} for name in _COMPONENT_CLASS_NAMES
}


def register_compatible_component_types(
    component_types: Mapping[str, type[Any]],
) -> tuple[tuple[str, type[Any]], ...]:
    """登记一个外部 Astr 单元实际导入的已知组件类。

    Astr 的 SDK 模块是进程级导入面。单元已经导入后若另一个隔离加载器刷新
    ``astrbot.*``，旧单元仍可能持有旧类对象。本函数只登记经单元准入捕获的
    已知组件类，使这些对象仍走既有的严格 OneBot 映射；它不会按类名接受任意
    未登记的第三方对象。

    Args:
        component_types: 组件类名到实际类对象的映射。

    Returns:
        可在单元卸载时传给 :func:`unregister_compatible_component_types` 的登记令牌。

    Raises:
        TypeError: 映射包含未知名称、非类对象或不匹配的组件 ``type`` 标识时抛出。
    """
    registrations: list[tuple[str, type[Any]]] = []
    for name, component_type in component_types.items():
        if name not in _COMPONENT_CLASS_NAMES:
            raise TypeError(f"不支持登记 Astr 消息组件类型: {name}")
        if not isinstance(component_type, type):
            raise TypeError(f"Astr 消息组件 {name} 必须是类对象")
        declared_type = str(getattr(component_type, "type", "") or "").lower()
        if declared_type not in _COMPONENT_TYPE_VALUES[name]:
            raise TypeError(
                f"Astr 消息组件 {name} 的 type 不匹配: {declared_type or '<empty>'}"
            )
        type_counts = _compatible_component_type_counts[name]
        type_counts[component_type] = type_counts.get(component_type, 0) + 1
        registrations.append((name, component_type))
    return tuple(registrations)


def unregister_compatible_component_types(
    registrations: tuple[tuple[str, type[Any]], ...],
) -> None:
    """移除一个已卸载 Astr 单元登记的组件类型。

    Args:
        registrations: :func:`register_compatible_component_types` 返回的登记令牌。
    """
    for name, component_type in registrations:
        registered = _compatible_component_type_counts.get(name)
        if registered is None:
            continue
        count = registered.get(component_type, 0)
        if count <= 1:
            registered.pop(component_type, None)
        else:
            registered[component_type] = count - 1


def _component_kind(component: Any) -> str | None:
    """识别标准或已登记兼容组件的严格类型。"""
    canonical_types: tuple[tuple[str, type[Any]], ...] = (
        ("Plain", Plain),
        ("At", At),
        ("Reply", Reply),
        ("Image", Image),
        ("Record", Record),
        ("Video", Video),
        ("File", File),
    )
    for name, component_type in canonical_types:
        if isinstance(component, component_type):
            return _COMPONENT_KIND_BY_CLASS_NAME[name]
    for name, registered_types in _compatible_component_type_counts.items():
        if any(isinstance(component, component_type) for component_type in registered_types):
            return _COMPONENT_KIND_BY_CLASS_NAME[name]
    return None


def _component_value(component: Any, attribute: str, component_name: str) -> Any:
    """读取已登记组件的必需字段，缺失时保持严格失败。"""
    if not hasattr(component, attribute):
        raise UnsupportedMessageSegmentError(
            f"AstrBot 消息组件 {component_name} 缺少 {attribute} 字段"
        )
    return getattr(component, attribute)


def chain_to_text(chain: list[BaseMessageComponent] | None) -> str:
    """拼接消息链中的文本组件，不对非文本组件做文本降级。"""
    return "".join(
        str(_component_value(item, "text", "Plain"))
        for item in chain or []
        if _component_kind(item) == "plain"
    )


def chain_to_onebot_segments(chain: list[BaseMessageComponent] | None) -> list[dict[str, Any]]:
    """将完整 AstrBot 消息链转换为 OneBot v11 原始段列表。

    Args:
        chain: 待发送的 AstrBot 消息组件列表。

    Returns:
        可直接传给 ``onebot_expand.MessageService`` 的 OneBot 段列表。

    Raises:
        UnsupportedMessageSegmentError: 任一段没有可验证的无损映射时抛出。
    """
    if not chain:
        raise UnsupportedMessageSegmentError("空消息链不能发送")

    segments: list[dict[str, Any]] = []
    reply_seen = False
    for component in chain:
        component_kind = _component_kind(component)
        if component_kind == "plain":
            segments.append(
                {
                    "type": "text",
                    "data": {
                        "text": str(_component_value(component, "text", "Plain")),
                    },
                }
            )
        elif component_kind == "at":
            target = str(_component_value(component, "qq", "At")).strip()
            if not target:
                raise UnsupportedMessageSegmentError("At 组件缺少目标 QQ 号")
            segments.append({"type": "at", "data": {"qq": target}})
        elif component_kind == "reply":
            message_id = str(_component_value(component, "id", "Reply")).strip()
            if not message_id:
                raise UnsupportedMessageSegmentError("Reply 组件缺少消息 ID")
            if reply_seen:
                raise UnsupportedMessageSegmentError("一条消息链只能包含一个 Reply 组件")
            reply_seen = True
            segments.append({"type": "reply", "data": {"id": _normalize_message_id(message_id)}})
        elif component_kind == "image":
            source = _media_source(
                _component_value(component, "url", "Image")
                or _component_value(component, "file", "Image"),
                "Image",
            )
            segments.append({"type": "image", "data": {"file": source}})
        elif component_kind == "record":
            source = _media_source(
                _component_value(component, "url", "Record")
                or _component_value(component, "file", "Record"),
                "Record",
            )
            segments.append({"type": "record", "data": {"file": source}})
        elif component_kind == "video":
            source = _media_source(
                _component_value(component, "url", "Video")
                or _component_value(component, "file", "Video"),
                "Video",
            )
            segments.append({"type": "video", "data": {"file": source}})
        elif component_kind == "file":
            source = _media_source(_component_value(component, "file", "File"), "File")
            data: dict[str, Any] = {"file": source}
            name = str(_component_value(component, "name", "File") or "")
            if name:
                data["name"] = name
            segments.append({"type": "file", "data": data})
        else:
            component_name = type(component).__name__
            raise UnsupportedMessageSegmentError(
                f"AstrBot 消息组件 {component_name} 没有可验证的 OneBot 映射"
            )

    if reply_seen:
        reply_index = next(index for index, item in enumerate(segments) if item["type"] == "reply")
        if reply_index:
            reply = segments.pop(reply_index)
            segments.insert(0, reply)
    return segments


def chain_to_segments(chain: list[BaseMessageComponent] | None) -> list[dict[str, Any]]:
    """兼容旧名称，返回严格的 OneBot 原始段列表。"""
    return chain_to_onebot_segments(chain)


def file_to_base64(path: str | Path) -> str:
    """读取本地文件并编码为 Base64 字符串。"""
    return base64.b64encode(Path(path).read_bytes()).decode("ascii")


def _normalize_message_id(message_id: str) -> int | str:
    """将数字形式的 OneBot 消息 ID 规范化为整数。"""
    try:
        return int(message_id)
    except ValueError:
        return message_id


def _media_source(value: str, component_name: str) -> str:
    """验证可传递到 OneBot 的媒体来源。"""
    normalized = str(value).strip()
    if not normalized:
        raise UnsupportedMessageSegmentError(f"{component_name} 组件缺少媒体来源")
    if normalized.startswith(("base64://", "http://", "https://", "file:///")):
        return normalized
    path = Path(normalized)
    if path.is_file():
        return f"file:///{path}"
    raise UnsupportedMessageSegmentError(
        f"{component_name} 组件的媒体来源不可验证: {normalized!r}"
    )


__all__ = [
    "At",
    "BaseMessageComponent",
    "ComponentTypes",
    "Face",
    "File",
    "Forward",
    "Image",
    "Music",
    "Node",
    "Nodes",
    "Plain",
    "Poke",
    "Record",
    "Reply",
    "Video",
    "chain_to_onebot_segments",
    "chain_to_segments",
    "chain_to_text",
    "file_to_base64",
    "register_compatible_component_types",
    "unregister_compatible_component_types",
]

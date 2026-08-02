"""AstrBot 消息事件与受限 OneBot 门面。"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any

from ..errors import OneBotProfileError
from .message_components import At, BaseMessageComponent, Image, Plain, Record, Reply, Video
from .message_event_result import MessageEventResult


@dataclass
class MessageMember:
    """AstrBot 发送者信息快照。"""

    user_id: str = ""
    nickname: str = ""
    cardname: str = ""


@dataclass
class Group:
    """AstrBot 群信息快照。"""

    id: str = ""
    name: str = ""


@dataclass
class PlatformMetadata:
    """AstrBot 平台元数据快照。"""

    id: str = ""
    name: str = ""
    type: str = ""


class MessageType:
    """AstrBot 消息类型常量。"""

    GROUP_MESSAGE = "group"
    PRIVATE_MESSAGE = "private"
    NOTICE_MESSAGE = "notice"
    OTHER_MESSAGE = "other"


@dataclass
class AstrBotMessage:
    """从宿主消息构建的只读 AstrBot 消息快照。"""

    type: str = MessageType.PRIVATE_MESSAGE
    self_id: str = ""
    session_id: str = ""
    message_id: str = ""
    group_id: str = ""
    sender: MessageMember = field(default_factory=MessageMember)
    message: list[BaseMessageComponent] = field(default_factory=list)
    message_str: str = ""
    raw_message: Any = None
    timestamp: int = 0


@dataclass
class MessageSesion:
    """AstrBot 会话标识兼容对象。"""

    platform_name: str = ""
    message_type: str = ""
    session_id: str = ""

    def __str__(self) -> str:
        """以稳定 AstrBot 风格格式表示会话。"""
        return f"{self.platform_name}:{self.message_type}:{self.session_id}"


class OneBotBotFacade:
    """通过 onebot_expand 公开 Service 调用的受限 OneBot 门面。"""

    def __init__(self, bridge: Any, message: Any) -> None:
        """绑定当前桥和产生此事件的 QQ 消息。"""
        self._bridge = bridge
        self._message = message

    @property
    def api(self) -> "OneBotBotFacade":
        """兼容 aiocqhttp 的 ``event.bot.api`` 调用形状。"""
        return self

    async def get_stranger_info(self, user_id: int | str, no_cache: bool = False) -> dict[str, Any]:
        """查询陌生人资料并保留原始 OneBot 响应。"""
        return await self._bridge.onebot_get_stranger_info(self._message, user_id, no_cache)

    async def get_group_member_info(
        self,
        group_id: int | str,
        user_id: int | str,
        no_cache: bool = False,
    ) -> dict[str, Any]:
        """查询群成员资料并保留原始 OneBot 响应。"""
        return await self._bridge.onebot_get_group_member_info(
            self._message, group_id, user_id, no_cache
        )

    async def send_group_msg(
        self,
        group_id: int | str,
        message: list[dict[str, Any]],
        auto_escape: bool = False,
    ) -> dict[str, Any]:
        """向群发送严格 OneBot 原始段。"""
        return await self._bridge.onebot_send_group_msg(
            self._message, group_id, message, auto_escape
        )

    async def send_private_msg(
        self,
        user_id: int | str,
        message: list[dict[str, Any]],
        auto_escape: bool = False,
    ) -> dict[str, Any]:
        """向私聊发送严格 OneBot 原始段。"""
        return await self._bridge.onebot_send_private_msg(
            self._message, user_id, message, auto_escape
        )

    async def delete_msg(self, message_id: int | str) -> dict[str, Any]:
        """撤回消息。"""
        return await self._bridge.onebot_delete_msg(self._message, message_id)

    async def get_msg(self, message_id: int | str) -> dict[str, Any]:
        """读取消息详情。"""
        return await self._bridge.onebot_get_msg(self._message, message_id)

    async def get_forward_msg(self, message_id: int | str) -> dict[str, Any]:
        """读取合并转发详情。"""
        return await self._bridge.onebot_get_forward_msg(self._message, message_id)

    async def call_action(self, action: str, **params: Any) -> dict[str, Any]:
        """将白名单内 OneBot action 分派到宿主公开 Service。"""
        if action == "get_stranger_info":
            return await self.get_stranger_info(
                params["user_id"], bool(params.get("no_cache", False))
            )
        if action == "get_group_member_info":
            return await self.get_group_member_info(
                params["group_id"],
                params["user_id"],
                bool(params.get("no_cache", False)),
            )
        if action == "get_group_member_list":
            return await self._bridge.onebot_get_group_member_list(
                self._message,
                params["group_id"],
                bool(params.get("no_cache", False)),
            )
        if action == "get_group_info":
            return await self._bridge.onebot_get_group_info(
                self._message,
                params["group_id"],
                bool(params.get("no_cache", False)),
            )
        if action == "send_group_msg":
            return await self.send_group_msg(
                params["group_id"],
                params["message"],
                bool(params.get("auto_escape", False)),
            )
        if action == "send_private_msg":
            return await self.send_private_msg(
                params["user_id"],
                params["message"],
                bool(params.get("auto_escape", False)),
            )
        if action == "delete_msg":
            return await self.delete_msg(params["message_id"])
        if action == "get_msg":
            return await self.get_msg(params["message_id"])
        if action == "get_forward_msg":
            return await self.get_forward_msg(params["message_id"])
        raise OneBotProfileError(f"未知 OneBot action: {action}")


class AstrMessageEvent:
    """包装宿主 Message 的 AstrBot 事件视图。"""

    def __init__(
        self,
        bridge: Any,
        message: Any,
        umo: str,
        platform_name: str = "astr_compat",
        unit: Any | None = None,
    ) -> None:
        """构建与原始宿主对象隔离的事件快照。"""
        self.bridge = bridge
        self._message = message
        self._umo = umo
        self._unit = unit
        self._stopped = False
        self._should_call_llm = True
        self.platform_name = platform_name
        raw_message = getattr(message, "raw_data", None)
        self.message_obj = AstrBotMessage(
            type=_message_type(message),
            self_id=str(_extra(message, "self_id")),
            session_id=umo,
            message_id=str(getattr(message, "message_id", "") or ""),
            group_id=str(_extra(message, "group_id")),
            sender=MessageMember(
                user_id=str(getattr(message, "sender_id", "") or ""),
                nickname=str(getattr(message, "sender_name", "") or ""),
                cardname=str(getattr(message, "sender_cardname", "") or ""),
            ),
            message=_message_components(message, unit=unit),
            message_str=str(getattr(message, "processed_plain_text", "") or getattr(message, "content", "") or ""),
            raw_message=raw_message,
            timestamp=int(float(getattr(message, "time", 0) or 0)),
        )

    @property
    def message_str(self) -> str:
        """返回快照中的纯文本。"""
        return self.message_obj.message_str

    @property
    def raw_message(self) -> Any:
        """返回宿主保留的原始协议数据。"""
        return self.message_obj.raw_message

    @property
    def unified_msg_origin(self) -> str:
        """返回来源聊天流标识。"""
        return self._umo

    @property
    def is_at_or_wake_command(self) -> bool:
        """返回宿主明确提供的 @ 或唤醒命令标志。"""
        extra = getattr(self._message, "extra", {})
        if not isinstance(extra, dict):
            return False
        nested = extra.get("extra")
        return bool(
            extra.get("is_at_or_wake_command", False)
            or (
                isinstance(nested, dict)
                and nested.get("is_at_or_wake_command", False)
            )
        )

    def get_sender_id(self) -> str:
        """返回消息发送者 ID。"""
        return self.message_obj.sender.user_id

    def get_sender_name(self) -> str:
        """返回消息发送者名称。"""
        return self.message_obj.sender.nickname or self.message_obj.sender.user_id

    def get_self_id(self) -> str:
        """返回机器人 ID（若宿主消息包含）。"""
        return self.message_obj.self_id

    def get_group_id(self) -> str:
        """返回群 ID；私聊为空字符串。"""
        return self.message_obj.group_id

    def get_session_id(self) -> str:
        """返回来源会话 ID。"""
        return self._umo

    def get_message_id(self) -> str:
        """返回来源消息 ID。"""
        return self.message_obj.message_id

    def get_platform_name(self) -> str:
        """返回 AstrBot 平台适配器名称。"""
        return self.platform_name

    def is_private_chat(self) -> bool:
        """判断当前消息是否来自私聊。"""
        return self.message_obj.type == MessageType.PRIVATE_MESSAGE

    def is_stopped(self) -> bool:
        """返回该外部事件是否已阻断。"""
        return self._stopped

    def stop_event(self) -> None:
        """阻断当前单元的后续处理器和宿主默认流程。"""
        self._stopped = True

    def should_call_llm(self, flag: bool | None = None) -> bool:
        """读取或设置当前事件是否允许进入 LLM。"""
        if flag is not None:
            self._should_call_llm = bool(flag)
        return self._should_call_llm

    def plain_result(self, text: str) -> MessageEventResult:
        """构造纯文本处理结果。"""
        return MessageEventResult.plain(text)

    def chain_result(self, chain: list[BaseMessageComponent]) -> MessageEventResult:
        """构造保留完整组件链的处理结果。"""
        return MessageEventResult.chain_result(chain)

    def image_result(self, url: str) -> MessageEventResult:
        """构造图片处理结果。"""
        return MessageEventResult.image(url)

    async def send(self, result: MessageEventResult) -> bool:
        """通过持有该事件的外部单元发送完整结果链。"""
        return await self.bridge.send_result(result, self._message, unit=self._unit)

    def is_admin(self) -> bool:
        """通过宿主消息只读角色映射判断管理员。"""
        return str(getattr(self._message, "sender_role", "") or "").lower() in {
            "owner",
            "operator",
            "admin",
        }


class AiocqhttpMessageEvent(AstrMessageEvent):
    """仅适用于 QQ/OneBot profile 的 AstrBot 事件视图。"""

    def __init__(
        self,
        bridge: Any,
        message: Any,
        umo: str,
        platform_name: str = "aiocqhttp",
        unit: Any | None = None,
    ) -> None:
        """验证 QQ profile 后附加受限 OneBot 门面。"""
        if str(getattr(message, "platform", "") or "").lower() != "qq":
            raise OneBotProfileError("AiocqhttpMessageEvent 只能用于 platform='qq' 的消息")
        super().__init__(bridge, message, umo, platform_name, unit)
        self.bot = OneBotBotFacade(bridge, message)

    def get_messages(self) -> list[BaseMessageComponent]:
        """返回已由宿主已知字段构建的组件快照。"""
        return list(self.message_obj.message)

    async def _parse_onebot_json(self, chain: Any) -> list[dict[str, Any]]:
        """将 MessageChain 转为可直接传给 OneBot Service 的段列表。"""
        if hasattr(chain, "to_segments"):
            return chain.to_segments()
        raise TypeError("_parse_onebot_json() 仅支持具有 to_segments() 的消息链")


def _extra(message: Any, key: str) -> Any:
    """安全读取 Message.extra 中的字段。"""
    extra = getattr(message, "extra", {})
    return extra.get(key, "") if isinstance(extra, dict) else ""


def _message_type(message: Any) -> str:
    """把宿主聊天/消息类型映射到 AstrBot 类型。"""
    raw_data = getattr(message, "raw_data", None)
    if isinstance(raw_data, dict) and raw_data.get("post_type") == "notice":
        return MessageType.NOTICE_MESSAGE
    chat_type = str(getattr(message, "chat_type", "") or "").lower()
    if chat_type == "group":
        return MessageType.GROUP_MESSAGE
    if chat_type == "private":
        return MessageType.PRIVATE_MESSAGE
    return MessageType.OTHER_MESSAGE


def _message_components(
    message: Any, *, unit: Any | None = None
) -> list[BaseMessageComponent]:
    """从公开 Message 字段构建已知组件快照。"""
    component_modules = (
        sys.modules.get("astrbot.api.message_components"),
        sys.modules.get("astrbot.core.message.components"),
    )

    def component_type(name: str, fallback: type[Any]) -> type[Any]:
        """从所属单元或当前 SDK 的公开导入路径取得组件类型。"""
        resolver = getattr(unit, "get_message_component_type", None)
        if callable(resolver):
            resolved = resolver(name, fallback)
            if isinstance(resolved, type):
                return resolved
        for module in component_modules:
            candidate = getattr(module, name, None)
            if isinstance(candidate, type):
                return candidate
        return fallback

    plain_type = component_type("Plain", Plain)
    at_type = component_type("At", At)
    image_type = component_type("Image", Image)
    record_type = component_type("Record", Record)
    reply_type = component_type("Reply", Reply)
    video_type = component_type("Video", Video)
    raw_data = getattr(message, "raw_data", None)
    raw_segments = raw_data.get("message") if isinstance(raw_data, dict) else None
    components: list[BaseMessageComponent] = []
    if isinstance(raw_segments, list):
        for segment in raw_segments:
            if not isinstance(segment, dict):
                continue
            segment_type = str(segment.get("type", "") or "").lower()
            data = segment.get("data", {})
            if not isinstance(data, dict):
                data = {}
            if segment_type == "text":
                components.append(plain_type(text=str(data.get("text", ""))))
            elif segment_type == "at":
                components.append(at_type(qq=data.get("qq", data.get("target_user_id", ""))))
            elif segment_type == "reply":
                components.append(reply_type(id=str(data.get("id", ""))))
            elif segment_type == "image":
                source = str(data.get("url", data.get("file", "")))
                if source:
                    components.append(image_type(file=source, url=str(data.get("url", ""))))
            elif segment_type in {"record", "voice"}:
                source = str(data.get("url", data.get("file", "")))
                if source:
                    components.append(record_type(file=source, url=str(data.get("url", ""))))
            elif segment_type == "video":
                source = str(data.get("url", data.get("file", "")))
                if source:
                    components.append(video_type(file=source, url=str(data.get("url", ""))))
    text = str(getattr(message, "processed_plain_text", "") or getattr(message, "content", "") or "")
    has_text_segment = any(isinstance(component, plain_type) for component in components)
    if text and not has_text_segment:
        components.insert(0, plain_type(text=text))
    return components


__all__ = [
    "AiocqhttpMessageEvent",
    "AstrBotMessage",
    "AstrMessageEvent",
    "Group",
    "MessageMember",
    "MessageSesion",
    "MessageType",
    "OneBotBotFacade",
    "PlatformMetadata",
]

"""AstrBot 兼容 SDK 的 provider 与配置对象。"""

from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Mapping
from typing import Any

from ..errors import ConfigPersistenceUnsupportedError, UnknownProviderIdError


@dataclass
class ProviderMetaData:
    """AstrBot 4.29.3 provider 元数据。"""

    id: str = ""
    name: str = ""
    type: str = ""
    model: str | None = None
    provider_type: Any = None
    desc: str = ""
    cls_type: Any = None
    default_config_tmpl: dict[str, Any] | None = None
    provider_display_name: str | None = None


class Provider:
    """AstrBot provider 兼容基类。"""

    def __init__(self, bridge: Any, provider_id: str = "") -> None:
        """创建已显式映射到宿主模型任务的 Provider 视图。"""
        if not bridge.has_provider(provider_id):
            raise UnknownProviderIdError(f"未配置 AstrBot Provider ID: {provider_id!r}")
        self._bridge = bridge
        self._id = provider_id

    def meta(self) -> ProviderMetaData:
        """返回 provider 元数据。"""
        return ProviderMetaData(id=self._id, name=self._id, type="chat")

    async def text_chat(
        self,
        prompt: str | None = None,
        image_urls: list[str] | None = None,
        audio_urls: list[str] | None = None,
        func_tool: Any = None,
        contexts: list[dict[str, Any]] | None = None,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> Any:
        """调用 LLM 生成文本。"""
        return await self._bridge.llm_text_chat(
            provider_id=self._id,
            prompt=prompt or "",
            image_urls=image_urls or [],
            audio_urls=audio_urls or [],
            tools=func_tool,
            contexts=contexts or [],
            system_prompt=system_prompt or "",
            unit=None,
            **kwargs,
        )


@dataclass
class LLMResponse:
    """AstrBot 4.29.3 LLM 响应字段兼容对象。"""

    content: str = ""
    role: str = "assistant"
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    reasoning_content: str | None = None
    reasoning_signature: str | None = None
    tools_call_args: list[dict[str, Any]] = field(default_factory=list)
    tools_call_name: list[str] = field(default_factory=list)
    tools_call_ids: list[str] = field(default_factory=list)
    tools_call_extra_content: dict[str, dict[str, Any]] = field(default_factory=dict)
    result_chain: Any = None
    raw_completion: Any = None
    raw: Any = None
    is_chunk: bool = False
    id: str | None = None
    usage: Any = None

    @property
    def completion_text(self) -> str:
        """Return the normalized plain-text response."""
        if self.result_chain is not None and hasattr(self.result_chain, "get_plain_text"):
            return str(self.result_chain.get_plain_text())
        return self.content

    @completion_text.setter
    def completion_text(self, value: str | None) -> None:
        self.content = str(value or "")


@dataclass
class ProviderRequest:
    """LLM 请求对象。"""

    prompt: str = ""
    image_urls: list[str] = field(default_factory=list)
    audio_urls: list[str] = field(default_factory=list)
    func_tool: Any = None
    contexts: list[dict[str, Any]] = field(default_factory=list)
    system_prompt: str = ""
    extra_user_content_parts: list[Any] = field(default_factory=list)
    payloads: list[Any] = field(default_factory=list)
    tools: list[Any] = field(default_factory=list)
    stream: bool = False
    meta_data: dict[str, Any] = field(default_factory=dict)


class AstrBotConfig(dict):
    """AstrBot 配置对象（dict 子类）。"""

    def __getattr__(self, name: str) -> Any:
        """兼容 AstrBot 配置节点的属性读取，并递归包装嵌套字典。"""
        try:
            value = self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc
        return _as_config_value(value)

    def save_config(self) -> None:
        """拒绝未映射的配置持久化请求。"""
        raise ConfigPersistenceUnsupportedError(
            "AstrBot 外部配置持久化尚无公开的宿主原子配置接口"
        )


class AstrBotConfigView(dict):
    """不带持久化能力的嵌套 AstrBot 配置只读/内存视图。"""

    def __getattr__(self, name: str) -> Any:
        """返回嵌套配置值或标准属性错误。"""
        try:
            value = self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc
        return _as_config_value(value)


def _as_config_value(value: Any) -> Any:
    """把嵌套映射转换为兼容属性读取的配置视图。"""
    if isinstance(value, Mapping) and not isinstance(value, AstrBotConfigView):
        return AstrBotConfigView(value)
    return value


class Personality:
    """AstrBot 人格对象。"""

    def __init__(self, persona_id: str = "", prompt: str = "") -> None:
        self.persona_id = persona_id
        self.prompt = prompt


__all__ = [
    "AstrBotConfig",
    "LLMResponse",
    "Personality",
    "Provider",
    "ProviderMetaData",
    "ProviderRequest",
]

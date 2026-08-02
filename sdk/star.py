"""AstrBot 兼容 SDK 的 Star 基类与受限 Context。"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Coroutine
from typing import Any

from ..errors import AgentLoopUnsupportedError, UnsupportedAstrCapabilityError
from .message_event_result import MessageChain


def register(
    name: str = "",
    author: str = "",
    desc: str = "",
    version: str = "",
    *args: Any,
    **kwargs: Any,
) -> Any:
    """记录 AstrBot ``@register`` 元数据而不改变桥的目录发现模型。"""
    del args, kwargs

    def decorator(cls: type) -> type:
        """把可选元数据保存到外部 Star 类。"""
        if name:
            cls.name = name
        if author:
            cls.author = author
        if desc:
            cls.plugin_description = desc
        if version:
            cls.plugin_version = version
        return cls

    return decorator


class Star:
    """AstrBot 外部插件的最小基类。"""

    author: str = ""
    name: str = ""
    context: "Context"
    logger: logging.Logger

    def __init__(self, context: "Context", config: dict[str, Any] | None = None) -> None:
        """保存桥上下文和配置快照。"""
        self.context = context
        self.config = config or {}
        self.logger = logging.getLogger(f"astr_compat.{self.__class__.__name__}")

    async def initialize(self) -> None:
        """外部单元激活钩子。"""

    async def terminate(self) -> None:
        """外部单元卸载钩子。"""

    async def text_to_image(self, text: str, return_url: bool = True) -> str:
        """拒绝没有宿主渲染器的文本转图片调用。"""
        del text, return_url
        raise UnsupportedAstrCapabilityError("宿主未提供 AstrBot text_to_image 的等价能力")

    async def html_render(
        self,
        tmpl: str,
        data: dict[str, Any],
        return_url: bool = True,
        options: dict[str, Any] | None = None,
    ) -> str:
        """拒绝没有宿主渲染器的 HTML 渲染调用。"""
        del tmpl, data, return_url, options
        raise UnsupportedAstrCapabilityError("宿主未提供 AstrBot html_render 的等价能力")


class Context:
    """按单元绑定的 AstrBot Context 兼容视图。"""

    def __init__(self, bridge: Any, unit: Any | None = None) -> None:
        """保存桥与当前外部单元引用。"""
        self._bridge = bridge
        self._unit = unit

    @property
    def platform_manager(self) -> Any:
        """拒绝未映射的平台管理器访问。"""
        raise UnsupportedAstrCapabilityError("platform_manager 未在 Astr 兼容桥中映射")

    @property
    def provider_manager(self) -> Any:
        """返回仅含已显式映射 Provider 的管理器。"""
        return self._bridge.provider_manager

    @property
    def conversation_manager(self) -> Any:
        """拒绝未映射的会话管理器访问。"""
        raise UnsupportedAstrCapabilityError("conversation_manager 未在 Astr 兼容桥中映射")

    @property
    def persona_manager(self) -> Any:
        """拒绝未映射的人格管理器访问。"""
        raise UnsupportedAstrCapabilityError("persona_manager 未在 Astr 兼容桥中映射")

    @property
    def message_history_manager(self) -> Any:
        """拒绝未映射的消息历史管理器访问。"""
        raise UnsupportedAstrCapabilityError("message_history_manager 未在 Astr 兼容桥中映射")

    @property
    def knowledge_base_manager(self) -> Any:
        """拒绝未映射的知识库管理器访问。"""
        raise UnsupportedAstrCapabilityError("knowledge_base_manager 未在 Astr 兼容桥中映射")

    @property
    def cron_manager(self) -> Any:
        """拒绝未映射的定时任务管理器访问。"""
        raise UnsupportedAstrCapabilityError("cron_manager 未在 Astr 兼容桥中映射")

    def get_config(self, umo: str | None = None) -> dict[str, Any]:
        """返回桥配置与当前单元配置的只读快照副本。"""
        del umo
        return self._bridge.get_config_for_unit(self._unit)

    async def send_message(
        self,
        session: str | Any,
        message_chain: MessageChain | list[Any],
    ) -> bool:
        """将完整组件链按来源会话发送，不进行文本降级。"""
        umo = str(session)
        if isinstance(message_chain, MessageChain):
            segments = message_chain.to_segments()
        else:
            chain = list(message_chain)
            segments = MessageChain(chain=chain).to_segments()
        return await self._bridge.send_to_umo(umo, segments=segments, unit=self._unit)

    def get_using_provider(self, umo: str) -> Any:
        """返回当前桥配置中的默认 Provider。"""
        del umo
        return self._bridge.get_using_provider()

    async def llm_generate(
        self,
        *,
        chat_provider_id: str,
        prompt: str | None = None,
        image_urls: list[str] | None = None,
        audio_urls: list[str] | None = None,
        tools: Any = None,
        system_prompt: str | None = None,
        contexts: list[Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        """执行具有显式 Provider 映射的宿主 LLM 请求。"""
        context_list: list[dict[str, Any]] = []
        for item in contexts or []:
            if isinstance(item, dict):
                context_list.append(item)
            elif hasattr(item, "model_dump"):
                context_list.append(item.model_dump())
            else:
                context_list.append({"role": "user", "content": str(item)})
        return await self._bridge.llm_text_chat(
            provider_id=chat_provider_id,
            prompt=prompt or "",
            image_urls=image_urls or [],
            audio_urls=audio_urls or [],
            tools=tools,
            contexts=context_list,
            system_prompt=system_prompt or "",
            unit=self._unit,
            **kwargs,
        )

    async def tool_loop_agent(
        self,
        *,
        event: Any,
        chat_provider_id: str,
        prompt: str | None = None,
        image_urls: list[str] | None = None,
        audio_urls: list[str] | None = None,
        tools: Any = None,
        system_prompt: str | None = None,
        contexts: list[Any] | None = None,
        max_steps: int = 30,
        **kwargs: Any,
    ) -> Any:
        """拒绝缺少宿主等价执行器的多轮 Agent 循环。"""
        del (
            event,
            chat_provider_id,
            prompt,
            image_urls,
            audio_urls,
            tools,
            system_prompt,
            contexts,
            max_steps,
            kwargs,
        )
        raise AgentLoopUnsupportedError("宿主未提供可验证的 AstrBot Agent 工具循环")

    def get_provider_by_id(self, provider_id: str) -> Any:
        """返回指定的显式 Provider 映射。"""
        return self._bridge.get_provider_by_id(provider_id)

    def get_all_providers(self) -> list[Any]:
        """返回所有已配置的 Provider。"""
        return self._bridge.get_all_providers()

    def get_using_stt_provider(self, umo: str) -> Any:
        """拒绝未映射的 STT Provider。"""
        del umo
        raise UnsupportedAstrCapabilityError("STT Provider 未在 Astr 兼容桥中映射")

    def get_using_tts_provider(self, umo: str) -> Any:
        """拒绝未映射的 TTS Provider。"""
        del umo
        raise UnsupportedAstrCapabilityError("TTS Provider 未在 Astr 兼容桥中映射")

    def get_all_stt_providers(self) -> list[Any]:
        """拒绝未映射的 STT Provider 列表请求。"""
        raise UnsupportedAstrCapabilityError("STT Provider 未在 Astr 兼容桥中映射")

    def get_all_tts_providers(self) -> list[Any]:
        """拒绝未映射的 TTS Provider 列表请求。"""
        raise UnsupportedAstrCapabilityError("TTS Provider 未在 Astr 兼容桥中映射")

    def get_all_embedding_providers(self) -> list[Any]:
        """拒绝未映射的 embedding Provider 列表请求。"""
        raise UnsupportedAstrCapabilityError("embedding Provider 未在 Astr 兼容桥中映射")

    def get_llm_tool_manager(self) -> Any:
        """返回当前单元预备期声明的工具集合。"""
        return self._bridge.get_llm_tool_manager(self._unit)

    def activate_llm_tool(self, name: str) -> bool:
        """启用当前单元已存在的工具。"""
        return self._bridge.activate_llm_tool(name, self._unit)

    def deactivate_llm_tool(self, name: str) -> bool:
        """禁用当前单元已存在的工具。"""
        return self._bridge.deactivate_llm_tool(name, self._unit)

    def add_llm_tools(self, *tools: Any) -> None:
        """只允许在预备阶段声明 FunctionTool。"""
        self._bridge.add_llm_tools(*tools, unit=self._unit)

    def get_registered_star(self, star_name: str) -> Any:
        """返回同桥内已加载的指定 Star。"""
        return self._bridge.get_registered_star(star_name)

    def get_all_stars(self) -> list[Any]:
        """返回同桥内全部外部 Star 实例。"""
        return self._bridge.get_all_stars()

    def get_platform(self, adapter_type: Any) -> Any:
        """拒绝创建或获取任意 AstrBot 平台实例。"""
        del adapter_type
        raise UnsupportedAstrCapabilityError("任意 AstrBot 平台实例未在桥中映射")

    def register_web_api(
        self,
        route: str,
        handler: Any,
        methods: list[str] | None = None,
        desc: str = "",
    ) -> None:
        """登记由桥静态 Router 派发的受限 Web API。"""
        self._bridge.register_web_api(route, handler, methods, desc, unit=self._unit)

    def register_task(self, coro: Coroutine[Any, Any, Any]) -> None:
        """使用宿主 TaskManager 创建并跟踪当前单元任务。"""
        if not inspect.iscoroutine(coro):
            raise TypeError("Context.register_task() 仅接受协程对象")
        self._bridge.register_task(coro, unit=self._unit)


__all__ = ["Context", "Star", "register"]

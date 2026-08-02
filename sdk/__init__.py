"""AstrBot 兼容 SDK 包入口。

本包在导入时被安装到 ``sys.modules``，使第三方 AstrBot Star 插件可以
通过 ``from astrbot.api import ...`` 正常导入。
"""

from __future__ import annotations

import logging
import sys
import tempfile
import types
from pathlib import Path

from . import filter as _filter
from .astr_message_event import (
    AiocqhttpMessageEvent,
    AstrBotMessage,
    AstrMessageEvent,
    Group,
    MessageMember,
    MessageSesion,
    MessageType,
    PlatformMetadata,
)
from .func_tool import FunctionTool, ToolSet
from .message_components import *  # noqa: F401, F403
from .message_event_result import (
    CommandResult,
    EventResultType,
    MessageChain,
    MessageEventResult,
    ResultContentType,
)
from .provider import AstrBotConfig, LLMResponse, Personality, Provider, ProviderMetaData, ProviderRequest
from .star import Context, Star, register
from .filter import (
    CommandGroup,
    CustomFilter,
    EventMessageType,
    PermissionType,
    PermissionTypeFilter,
    PlatformAdapterType,
    after_message_sent,
    command,
    command_group,
    custom_filter,
    event_message_type,
    llm_tool,
    on_agent_begin,
    on_agent_done,
    on_astrbot_loaded,
    on_decorating_result,
    on_llm_request,
    on_llm_response,
    on_llm_tool_respond,
    on_platform_loaded,
    on_plugin_error,
    on_plugin_loaded,
    on_plugin_unloaded,
    on_using_llm_tool,
    on_waiting_llm_request,
    permission_type,
    platform_adapter_type,
    regex,
)

logger = logging.getLogger("astrbot")


def install() -> None:
    """将兼容 SDK 安装到 sys.modules。"""
    if "astrbot" in sys.modules:
        installed_root = sys.modules["astrbot"]
        if not getattr(installed_root, "__mofox_compat__", False):
            raise RuntimeError("检测到已安装的 astrbot 模块；兼容桥不会覆盖真实 AstrBot 运行时")
        if getattr(installed_root, "__mofox_compat_source__", "") == __name__:
            return
        for module_name in list(sys.modules):
            if module_name == "astrbot" or module_name.startswith("astrbot."):
                sys.modules.pop(module_name, None)

    api = types.ModuleType("astrbot.api")
    api.__path__ = []
    # astrbot.api.event 必须是包装模块，才能支持
    # ``from astrbot.api.event import filter, AstrMessageEvent`` 与
    # ``from astrbot.api.event.filter import command`` 两种写法。
    event_pkg = types.ModuleType("astrbot.api.event")
    event_pkg.filter = _filter
    event_pkg.AstrMessageEvent = AstrMessageEvent
    event_pkg.MessageEventResult = MessageEventResult
    event_pkg.MessageChain = MessageChain
    event_pkg.CommandResult = CommandResult
    event_pkg.EventResultType = EventResultType
    event_pkg.ResultContentType = ResultContentType
    api.event = event_pkg
    api.filter = _filter
    api_star = types.ModuleType("astrbot.api.star")
    api_star.Context = Context
    api_star.Star = Star
    api_star.register = register
    api_star.star_filter = _filter
    api.star = api_star
    api_provider = types.ModuleType("astrbot.api.provider")
    api_provider.Provider = Provider
    api_provider.ProviderMetaData = ProviderMetaData
    api_provider.LLMResponse = LLMResponse
    api_provider.ProviderRequest = ProviderRequest
    api_provider.Personality = Personality
    api_provider.AstrBotConfig = AstrBotConfig
    api.provider = api_provider
    api_platform = types.ModuleType("astrbot.api.platform")
    api_platform.AstrBotMessage = AstrBotMessage
    api_platform.AstrMessageEvent = AstrMessageEvent
    api_platform.Group = Group
    api_platform.MessageMember = MessageMember
    api_platform.MessageType = MessageType
    api_platform.PlatformMetadata = PlatformMetadata
    api.platform = api_platform
    api.message_components = sys.modules[__name__]
    api.FunctionTool = FunctionTool
    api.AstrBotConfig = AstrBotConfig
    api.Personality = Personality
    api.MessageChain = MessageChain
    api.MessageEventResult = MessageEventResult
    api.logger = logger

    # astrbot.api.all：聚合常用符号，支持 ``from astrbot.api.all import *``
    api_all = types.ModuleType("astrbot.api.all")
    api_all.Star = Star
    api_all.Context = Context
    api_all.register = register
    api_all.star_filter = _filter
    api_all.filter = _filter
    api_all.Provider = Provider
    api_all.ProviderMetaData = ProviderMetaData
    api_all.ProviderRequest = ProviderRequest
    api_all.LLMResponse = LLMResponse
    api_all.Personality = Personality
    api_all.AstrBotConfig = AstrBotConfig
    api_all.AstrBotMessage = AstrBotMessage
    api_all.AstrMessageEvent = AstrMessageEvent
    api_all.MessageChain = MessageChain
    api_all.MessageEventResult = MessageEventResult
    api_all.MessageMember = MessageMember
    api_all.MessageType = MessageType
    api_all.Group = Group
    api_all.PlatformMetadata = PlatformMetadata
    api_all.FunctionTool = FunctionTool
    api_all.logger = logger
    api_all.__all__ = [
        "Star", "Context", "register", "star_filter", "filter",
        "Provider", "ProviderMetaData", "ProviderRequest", "LLMResponse",
        "Personality", "AstrBotConfig", "AstrBotMessage", "AstrMessageEvent",
        "MessageChain", "MessageEventResult", "MessageMember", "MessageType",
        "Group", "PlatformMetadata", "FunctionTool", "logger",
    ]
    api.all = api_all
    sys.modules.setdefault("astrbot.api.all", api_all)

    root = types.ModuleType("astrbot")
    root.__path__ = []
    root.api = api
    root.logger = logger
    root.__mofox_compat__ = True
    root.__mofox_compat_source__ = __name__

    # astrbot.core.* 命名空间，兼容部分插件深度导入的写法
    core = types.ModuleType("astrbot.core")
    core.__path__ = []  # 标记为包，使子模块导入可解析
    core.star = types.ModuleType("astrbot.core.star")
    core.star.Star = Star
    core.star.Context = Context
    core.star.register = register
    core.star.StarMetadata = types.SimpleNamespace
    core.platform = types.ModuleType("astrbot.core.platform")
    core.platform.astr_message_event = types.ModuleType("astrbot.core.platform.astr_message_event")
    core.platform.astr_message_event.AstrMessageEvent = AstrMessageEvent
    core.platform.astr_message_event.MessageSesion = MessageSesion
    core.platform.message_type = types.ModuleType("astrbot.core.platform.message_type")
    core.platform.message_type.MessageType = MessageType
    core.message = types.ModuleType("astrbot.core.message")
    core.message.message_event_result = types.ModuleType("astrbot.core.message.message_event_result")
    core.message.message_event_result.MessageChain = MessageChain
    core.message.message_event_result.MessageEventResult = MessageEventResult
    core.message.message_event_result.EventResultType = EventResultType
    core.message.message_event_result.ResultContentType = ResultContentType
    core.message.components = sys.modules[__name__]
    # astrbot.core.utils.* 命名空间（部分插件用于获取数据目录等）
    core.utils = types.ModuleType("astrbot.core.utils")
    core.utils.__path__ = []
    core.utils.astrbot_path = types.ModuleType("astrbot.core.utils.astrbot_path")

    def _get_astrbot_data_path() -> str:
        """兼容占位：返回插件自身 data 目录（无真实 AstrBot 运行时）。"""
        from pathlib import Path
        return str(Path.cwd() / "data")

    core.utils.astrbot_path.get_astrbot_data_path = _get_astrbot_data_path

    def _get_astrbot_plugin_data_path() -> str:
        """返回 AstrBot 插件共享数据根目录。"""
        plugin_data_path = Path(_get_astrbot_data_path()) / "plugin_data"
        plugin_data_path.mkdir(parents=True, exist_ok=True)
        return str(plugin_data_path)

    core.utils.astrbot_path.get_astrbot_plugin_data_path = _get_astrbot_plugin_data_path

    # ── aiocqhttp 平台命名空间（深度依赖插件的 import 兜底） ──────
    # 许多 AstrBot 插件直接 import ``aiocqhttp.CQHttp``（仅作类型注解）
    # 和 ``astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event.
    # AiocqhttpMessageEvent``。这里注册类型兼容模块；实际 OneBot 调用由
    # AiocqhttpMessageEvent.bot 的受限 OneBot 门面完成。
    aiocqhttp_mod = types.ModuleType("aiocqhttp")
    aiocqhttp_mod.CQHttp = type("CQHttp", (), {"__init__": lambda self, *a, **kw: None})
    aiocqhttp_mod.Event = type("Event", (), {})

    sources_pkg = types.ModuleType("astrbot.core.platform.sources")
    sources_pkg.__path__ = []
    aiocqhttp_source_pkg = types.ModuleType("astrbot.core.platform.sources.aiocqhttp")
    aiocqhttp_source_pkg.__path__ = []
    aiocqhttp_event_mod = types.ModuleType(
        "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event"
    )
    aiocqhttp_event_mod.AiocqhttpMessageEvent = AiocqhttpMessageEvent
    aiocqhttp_source_pkg.aiocqhttp_message_event = aiocqhttp_event_mod
    sources_pkg.aiocqhttp = aiocqhttp_source_pkg
    core.platform.sources = sources_pkg

    # astrbot.core.config.astrbot_config.AstrBotConfig
    core_config_pkg = types.ModuleType("astrbot.core.config")
    core_config_pkg.__path__ = []
    core_config_mod = types.ModuleType("astrbot.core.config.astrbot_config")
    core_config_mod.AstrBotConfig = AstrBotConfig
    core_config_pkg.astrbot_config = core_config_mod
    core.config = core_config_pkg

    # astrbot.core.star.filter（部分插件从此路径导入 filter，或其子模块）
    core_star_filter_mod = types.ModuleType("astrbot.core.star.filter")
    core_star_filter_mod.__path__ = []  # 标记为包，允许 from ...filter.xxx import
    core_star_filter_mod.PlatformAdapterType = PlatformAdapterType
    core_star_filter_mod.command = command
    core_star_filter_mod.regex = regex
    core_star_filter_mod.permission_type = permission_type
    core_star_filter_mod.platform_adapter_type = platform_adapter_type
    core_star_filter_mod.event_message_type = event_message_type
    core_star_filter_mod.llm_tool = llm_tool
    core.star.filter = core_star_filter_mod

    # filter 的子模块（部分插件 ``from astrbot.core.star.filter.xxx import Yyy``）
    core_star_filter_pat_mod = types.ModuleType(
        "astrbot.core.star.filter.platform_adapter_type"
    )
    core_star_filter_pat_mod.PlatformAdapterType = PlatformAdapterType
    core_star_filter_pt_mod = types.ModuleType(
        "astrbot.core.star.filter.permission_type"
    )
    core_star_filter_pt_mod.PermissionType = PermissionType
    core_star_filter_permission_mod = types.ModuleType(
        "astrbot.core.star.filter.permission"
    )
    core_star_filter_permission_mod.PermissionType = PermissionType
    core_star_filter_permission_mod.PermissionTypeFilter = PermissionTypeFilter
    core_star_filter_emt_mod = types.ModuleType(
        "astrbot.core.star.filter.event_message_type"
    )
    core_star_filter_emt_mod.EventMessageType = EventMessageType

    core_star_handler_mod = types.ModuleType("astrbot.core.star.star_handler")
    core_star_handler_mod.star_handlers_registry = []
    core.star.star_handler = core_star_handler_mod

    # astrbot.core.star.context.Context（部分插件直接从此路径导入 Context）
    core_star_context_mod = types.ModuleType("astrbot.core.star.context")
    core_star_context_mod.Context = Context
    core.star.context = core_star_context_mod

    # 补 get_astrbot_temp_path（部分插件用于拼临时文件路径）
    _astrbot_temp = Path(tempfile.gettempdir()) / "astrbot"
    _astrbot_temp.mkdir(parents=True, exist_ok=True)

    def _get_astrbot_temp_path() -> str:
        return str(_astrbot_temp)

    core.utils.astrbot_path.get_astrbot_temp_path = _get_astrbot_temp_path

    root.core = core

    sys.modules["astrbot"] = root
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event_pkg
    sys.modules["astrbot.api.event.filter"] = _filter
    sys.modules["astrbot.api.star"] = api_star
    sys.modules["astrbot.api.provider"] = api_provider
    sys.modules["astrbot.api.platform"] = api_platform
    sys.modules["astrbot.api.message_components"] = api.message_components
    sys.modules["astrbot.core"] = core
    sys.modules["astrbot.core.star"] = core.star
    sys.modules["astrbot.core.star.filter"] = core_star_filter_mod
    sys.modules["astrbot.core.star.filter.platform_adapter_type"] = core_star_filter_pat_mod
    sys.modules["astrbot.core.star.filter.permission_type"] = core_star_filter_pt_mod
    sys.modules["astrbot.core.star.filter.permission"] = core_star_filter_permission_mod
    sys.modules["astrbot.core.star.filter.event_message_type"] = core_star_filter_emt_mod
    sys.modules["astrbot.core.star.star_handler"] = core_star_handler_mod
    sys.modules["astrbot.core.star.context"] = core_star_context_mod
    sys.modules["astrbot.core.config"] = core_config_pkg
    sys.modules["astrbot.core.config.astrbot_config"] = core_config_mod
    sys.modules["astrbot.core.platform"] = core.platform
    sys.modules["astrbot.core.platform.sources"] = sources_pkg
    sys.modules["astrbot.core.platform.sources.aiocqhttp"] = aiocqhttp_source_pkg
    sys.modules["astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event"] = aiocqhttp_event_mod
    sys.modules["astrbot.core.platform.astr_message_event"] = core.platform.astr_message_event
    sys.modules["astrbot.core.platform.message_type"] = core.platform.message_type
    sys.modules["astrbot.core.message"] = core.message
    sys.modules["astrbot.core.message.message_event_result"] = core.message.message_event_result
    sys.modules["astrbot.core.message.components"] = core.message.components
    sys.modules["astrbot.core.utils"] = core.utils
    sys.modules["astrbot.core.utils.astrbot_path"] = core.utils.astrbot_path
    sys.modules["aiocqhttp"] = aiocqhttp_mod


__all__ = [
    "AstrBotConfig",
    "AstrBotMessage",
    "AstrMessageEvent",
    "AiocqhttpMessageEvent",
    "CommandResult",
    "CommandGroup",
    "Context",
    "CustomFilter",
    "EventResultType",
    "FunctionTool",
    "Group",
    "LLMResponse",
    "MessageChain",
    "MessageEventResult",
    "MessageMember",
    "MessageSesion",
    "MessageType",
    "Personality",
    "PlatformMetadata",
    "after_message_sent",
    "command",
    "command_group",
    "custom_filter",
    "event_message_type",
    "llm_tool",
    "on_agent_begin",
    "on_agent_done",
    "on_astrbot_loaded",
    "on_decorating_result",
    "on_llm_request",
    "on_llm_response",
    "on_llm_tool_respond",
    "on_platform_loaded",
    "on_plugin_error",
    "on_plugin_loaded",
    "on_plugin_unloaded",
    "on_using_llm_tool",
    "on_waiting_llm_request",
    "permission_type",
    "platform_adapter_type",
    "regex",
    "Provider",
    "ProviderMetaData",
    "ProviderRequest",
    "ResultContentType",
    "Star",
    "ToolSet",
    "install",
    "register",
]

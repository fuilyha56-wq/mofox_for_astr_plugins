"""AstrBot 兼容 SDK 的过滤器与事件类型枚举。"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from typing import Any, Callable


class EventMessageType(enum.Enum):
    """AstrBot 消息事件类型。"""

    ALL = "all"
    GROUP_MESSAGE = "group"
    PRIVATE_MESSAGE = "private"


class PlatformAdapterType(enum.Flag):
    """AstrBot 平台适配器类型。"""

    ALL = 0
    AIOCQHTTP = enum.auto()
    QQOFFICIAL = enum.auto()
    TELEGRAM = enum.auto()
    WECOM = enum.auto()
    LARK = enum.auto()
    DINGTALK = enum.auto()
    DISCORD = enum.auto()
    SLACK = enum.auto()
    KOOK = enum.auto()
    VOCECHAT = enum.auto()
    WEIXIN_OFFICIAL_ACCOUNT = enum.auto()
    SATORI = enum.auto()
    MISSKEY = enum.auto()
    LINE = enum.auto()


class PermissionType(enum.Enum):
    """权限类型。"""

    ADMIN = "admin"
    MEMBER = "member"


class PermissionTypeFilter:
    """AstrBot 权限过滤器兼容描述。"""

    def __init__(
        self,
        permission_type: PermissionType,
        raise_error: bool = True,
    ) -> None:
        """保存权限等级与拒绝时是否抛错。"""
        self.permission_type = permission_type
        self.raise_error = raise_error


@dataclass
class _Filter:
    """单个过滤器的描述。"""

    kind: str
    value: Any = None


class _FilterRegistry(list[_Filter]):
    """附加在处理函数上的过滤器集合。"""


def _attach(function: Callable[..., Any], filter_: _Filter) -> None:
    """将过滤器附加到处理函数。"""
    existing = getattr(function, "__mofox_astr_filters__", None)
    if not isinstance(existing, _FilterRegistry):
        existing = _FilterRegistry()
        setattr(function, "__mofox_astr_filters__", existing)
    existing.append(filter_)


def command(name: str, alias: set | None = None, **kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """注册指令过滤器。"""

    def decorator(function: Callable[..., Any]) -> Callable[..., Any]:
        _attach(function, _Filter("command", {"name": name, "alias": set(alias or ())}))
        return function

    return decorator


class CommandGroup:
    """指令组装饰器包装。

    AstrBot 插件写法：:

        @filter.command_group("math")
        def math(self): ...

        @math.command("add")
        async def add(self, event, a, b): ...

    本包装为 ``command`` / ``group`` 提供级联注册，并在 command 过滤器中
    记录父指令组路径，供运行时做前缀匹配。
    """

    def __init__(
        self,
        name: str,
        path: str,
        alias: set[str],
        path_aliases: tuple[set[str], ...] | None = None,
    ) -> None:
        """初始化命令组及其每级可接受名称。"""
        self.name = name
        self.path = path
        self.alias = alias
        self.path_aliases = path_aliases or ({name, *alias},)

    def __call__(self, function: Callable[..., Any]) -> Callable[..., Any]:
        """组本体不处理消息，仅声明组，并把级联方法绑定到函数。"""
        _attach(
            function,
            _Filter(
                "command_group",
                {
                    "path": self.path,
                    "alias": self.alias,
                    "path_aliases": self.path_aliases,
                },
            ),
        )
        setattr(function, "command", self.command)
        setattr(function, "group", self.group)
        return function

    def command(self, name: str, alias: set | None = None, **kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """注册组内子指令。"""
        return _group_command(self.path, name, alias or set(), self.path_aliases)

    def group(self, name: str, alias: set | None = None, **kwargs: Any) -> "CommandGroup":
        """注册嵌套子指令组。"""
        resolved_alias = set(alias or ())
        return CommandGroup(
            name,
            f"{self.path} {name}",
            resolved_alias,
            (*self.path_aliases, {name, *resolved_alias}),
        )


def _group_command(
    group_path: str,
    name: str,
    alias: set[str],
    path_aliases: tuple[set[str], ...],
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """构造带父指令组路径的 command 过滤器。"""

    def decorator(function: Callable[..., Any]) -> Callable[..., Any]:
        _attach(
            function,
            _Filter(
                "command",
                {
                    "name": name,
                    "alias": alias,
                    "group_path": group_path,
                    "group_path_aliases": path_aliases,
                },
            ),
        )
        return function

    return decorator


def command_group(name: str, alias: set | None = None, **kwargs: Any) -> CommandGroup:
    """注册指令组过滤器。"""
    del kwargs
    return CommandGroup(name, name, set(alias or ()))


def regex(pattern: str | re.Pattern, **kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """注册正则过滤器。"""

    compiled = re.compile(pattern) if isinstance(pattern, str) else pattern

    def decorator(function: Callable[..., Any]) -> Callable[..., Any]:
        _attach(function, _Filter("regex", compiled))
        return function

    return decorator


def event_message_type(message_type: EventMessageType, **kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """注册消息类型过滤器。"""

    def decorator(function: Callable[..., Any]) -> Callable[..., Any]:
        _attach(function, _Filter("event_message_type", message_type))
        return function

    return decorator


def platform_adapter_type(adapter_type: PlatformAdapterType, **kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """注册平台适配器过滤器。"""

    def decorator(function: Callable[..., Any]) -> Callable[..., Any]:
        _attach(function, _Filter("platform_adapter_type", adapter_type))
        return function

    return decorator


def permission_type(permission: PermissionType, raise_error: bool = True, **kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """注册权限过滤器。"""

    def decorator(function: Callable[..., Any]) -> Callable[..., Any]:
        _attach(function, _Filter("permission_type", {"level": permission, "raise_error": raise_error}))
        return function

    return decorator


def on_astrbot_loaded(**kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """注册 AstrBot 加载完成钩子。"""

    def decorator(function: Callable[..., Any]) -> Callable[..., Any]:
        _attach(function, _Filter("on_astrbot_loaded", None))
        return function

    return decorator


def _hook(kind: str) -> Callable[..., Callable[[Callable[..., Any]], Callable[..., Any]]]:
    """构造事件钩子装饰器。"""

    def decorator(**kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorate(function: Callable[..., Any]) -> Callable[..., Any]:
            _attach(function, _Filter(kind, None))
            return function

        return decorate

    return decorator


def on_platform_loaded(**kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """注册平台加载完成钩子。"""
    return _hook("on_platform_loaded")(**kwargs)


def on_plugin_loaded(**kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """注册插件加载完成钩子。"""
    return _hook("on_plugin_loaded")(**kwargs)


def on_plugin_unloaded(**kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """注册插件卸载完成钩子。"""
    return _hook("on_plugin_unloaded")(**kwargs)


def on_plugin_error(**kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """注册插件处理消息异常钩子。"""
    return _hook("on_plugin_error")(**kwargs)


def on_waiting_llm_request(**kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """注册等待 LLM 请求钩子。"""
    return _hook("on_waiting_llm_request")(**kwargs)


def on_agent_begin(**kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """注册 Agent 开始运行钩子。"""
    return _hook("on_agent_begin")(**kwargs)


def on_agent_done(**kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """注册 Agent 运行完成钩子。"""
    return _hook("on_agent_done")(**kwargs)


def on_using_llm_tool(**kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """注册 LLM 工具调用前钩子。"""
    return _hook("on_using_llm_tool")(**kwargs)


def on_llm_tool_respond(**kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """注册 LLM 工具调用后钩子。"""
    return _hook("on_llm_tool_respond")(**kwargs)


def on_llm_request(**kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """注册 LLM 请求前钩子。"""
    return _hook("on_llm_request")(**kwargs)


def on_llm_response(**kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """注册 LLM 响应钩子。"""
    return _hook("on_llm_response")(**kwargs)


def on_decorating_result(**kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """注册消息装饰钩子（发送前）。"""
    return _hook("on_decorating_result")(**kwargs)


def after_message_sent(**kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """注册消息发送后钩子。"""
    return _hook("after_message_sent")(**kwargs)


def llm_tool(name: str | None = None, **kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """注册 LLM 工具。"""

    def decorator(function: Callable[..., Any]) -> Callable[..., Any]:
        _attach(function, _Filter("llm_tool", {"name": name or function.__name__}))
        return function

    return decorator


class CustomFilter:
    """自定义过滤器基类。"""

    def filter(self, event: Any, cfg: Any) -> bool:  # noqa: A003
        """默认放行。"""
        return True


def custom_filter(filter_obj: CustomFilter, **kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """注册自定义过滤器。"""

    def decorator(function: Callable[..., Any]) -> Callable[..., Any]:
        _attach(function, _Filter("custom_filter", filter_obj))
        return function

    return decorator


__all__ = [
    "CustomFilter",
    "EventMessageType",
    "PermissionType",
    "PermissionTypeFilter",
    "PlatformAdapterType",
    "after_message_sent",
    "command",
    "command_group",
    "custom_filter",
    "event_message_type",
    "llm_tool",
    "on_astrbot_loaded",
    "on_decorating_result",
    "on_llm_request",
    "on_llm_response",
    "permission_type",
    "platform_adapter_type",
    "regex",
]

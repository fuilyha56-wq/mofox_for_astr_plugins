"""OneBot/Aiocqhttp 定向的 AstrBot Star 插件兼容桥。

该桥只支持可由 Neo-MoFox 公开 API 真实表达的 AstrBot 子集。外部插件先在
构造期预备并生成宿主可见组件，随后才在生命周期钩子中激活；无法映射的行为
均以稳定错误显式失败，绝不降级为假成功。
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import inspect
import json
import re
import sys
import types
from collections.abc import Callable, Coroutine, Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, get_origin

from fastapi import HTTPException, Request

from src.app.plugin_system.api import llm_api, permission_api, router_api, service_api
from src.core.components import (
    BaseEventHandler,
    BasePlugin,
    BaseRouter,
    BaseTool,
    register_plugin,
)
from src.core.components.types import (
    ChatType,
    EventType,
    PermissionLevel,
    build_signature,
    ComponentType,
)
from src.core.models.message import Message
from src.kernel.concurrency import get_task_manager
from src.kernel.event import EventDecision
from src.kernel.llm import LLMPayload, ROLE, Text
from src.kernel.logger import get_logger

from .config import AstrCompatConfig
from .config_merger import (
    ConfigChangePoller,
    apply_toml_data_to_units,
    build_merged_config_class,
    collect_plugin_sections,
    install_merged_config,
)
from .errors import (
    AstrUnitAdmissionError,
    LateComponentRegistrationError,
    OneBotOperationError,
    OneBotProfileError,
    UnitUnavailableError,
    UnknownProviderIdError,
    UnsupportedAstrCapabilityError,
)
from .sdk import install as install_sdk
from .sdk.astr_message_event import AiocqhttpMessageEvent, AstrMessageEvent
from .sdk.filter import EventMessageType, _Filter, _FilterRegistry
from .sdk.func_tool import FunctionTool, ToolSet
from .sdk.message_components import (
    register_compatible_component_types,
    unregister_compatible_component_types,
)
from .sdk.message_event_result import MessageEventResult
from .sdk.provider import AstrBotConfig, LLMResponse as CompatLLMResponse
from .sdk.provider import Provider, ProviderRequest
from .sdk.star import Context, Star

logger = get_logger("mofox_astr_compat")

_MESSAGE_SERVICE_SIGNATURE = "onebot_expand:service:message_service"
_ACCOUNT_SERVICE_SIGNATURE = "onebot_expand:service:account_service"
_SUPPORTED_WEB_METHODS = frozenset({"GET", "POST", "PUT", "DELETE", "PATCH"})
_SUPPORTED_HOOKS = frozenset(
    {"on_astrbot_loaded", "on_llm_request", "on_llm_response", "after_message_sent"}
)
# AstrBot 默认 wake_prefix 为 ["/"]；桥按此默认值推断唤醒状态并剥离前缀。
WAKE_PREFIXES: tuple[str, ...] = ("/",)
_REJECTED_HOOKS = frozenset(
    {
        "on_platform_loaded",
        "on_plugin_loaded",
        "on_plugin_unloaded",
        "on_plugin_error",
        "on_agent_begin",
        "on_agent_done",
        "on_waiting_llm_request",
        "on_using_llm_tool",
        "on_llm_tool_respond",
        "on_decorating_result",
    }
)


class AstrUnitState(str, Enum):
    """外部 AstrBot 单元的生命周期状态。"""

    DISCOVERED = "discovered"
    PREPARED = "prepared"
    REGISTERED = "registered"
    ACTIVE = "active"
    QUARANTINED = "quarantined"
    DRAINING = "draining"
    UNLOADED = "unloaded"


@dataclass
class AstrWebRoute:
    """由桥静态 Router 分派的外部 Web API 声明。"""

    unit: "AstrPluginUnit"
    route: str
    handler: Callable[..., Any]
    methods: tuple[str, ...]
    description: str = ""


class _ProviderManager:
    """仅暴露桥显式配置的 Provider 映射。"""

    def __init__(self, bridge: "AstrPluginBridge") -> None:
        """保存桥引用。"""
        self._bridge = bridge

    async def get_provider_by_id(self, provider_id: str) -> Provider:
        """异步兼容入口，返回已验证 Provider。"""
        return self._bridge.get_provider_by_id(provider_id)

    def get_all_providers(self) -> list[Provider]:
        """返回所有显式 Provider。"""
        return self._bridge.get_all_providers()

    @property
    def llm_tools(self) -> ToolSet:
        """返回桥全部预备期工具的只读集合。"""
        return self._bridge.llm_tool_manager


class AstrPluginUnit:
    """一个外部 AstrBot Star 插件的隔离运行时单元。"""

    def __init__(self, bridge: "AstrPluginBridge", plugin_dir: str, slug: str) -> None:
        """创建尚未导入的单元。"""
        self.bridge = bridge
        self.plugin_dir = str(Path(plugin_dir).resolve())
        self.slug = slug
        self.module_prefix = f"_mofox_astr_{slug}"
        self.state = AstrUnitState.DISCOVERED
        self.instance: Star | None = None
        self.context: Context | None = None
        self.module: types.ModuleType | None = None
        self._component_type_registrations: tuple[tuple[str, type[Any]], ...] = ()
        self._message_component_types: dict[str, type[Any]] = {}
        self.handlers: list[Callable[..., Any]] = []
        self.hook_handlers: dict[str, list[Callable[..., Any]]] = {
            hook: [] for hook in _SUPPORTED_HOOKS
        }
        self.llm_tool_handlers: list[tuple[str, Callable[..., Any]]] = []
        self._llm_tools = ToolSet()
        self._function_tools: list[FunctionTool] = []
        self.dynamic_tools: list[type[BaseTool]] = []
        self.config_dict: AstrBotConfig = AstrBotConfig()
        self.web_routes: list[AstrWebRoute] = []
        self.messages: dict[str, Message] = {}
        self.task_ids: set[str] = set()
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self.active_calls = 0
        self._lease_condition = asyncio.Condition()
        self.failure: str = ""

    def prepare(self) -> None:
        """在宿主组件快照前同步导入、实例化、准入和生成动态工具。"""
        if self.state is not AstrUnitState.DISCOVERED:
            raise AstrUnitAdmissionError(f"单元 {self.slug} 已完成或正在完成预备")
        main_file = Path(self.plugin_dir) / "main.py"
        if not main_file.is_file():
            raise AstrUnitAdmissionError(f"入口不存在: {main_file}")
        try:
            install_sdk()
            self._load_config(Path(self.plugin_dir))
            self.context = Context(self.bridge, self)
            self.module = self._import_module(main_file)
            self._capture_message_component_types()
            star_cls = self._find_star_class(self.module)
            self.instance = self._instantiate_star(star_cls)
            self._classify_declarations()
            self._validate_admission()
            self._build_dynamic_tools()
            self.state = AstrUnitState.PREPARED
        except Exception:
            self._cleanup_modules()
            raise

    async def activate(self) -> None:
        """调用外部初始化钩子并隔离失败单元。"""
        if self.state is not AstrUnitState.REGISTERED:
            raise UnitUnavailableError(f"单元 {self.slug} 未注册，不能激活")
        if self.instance is None:
            raise UnitUnavailableError(f"单元 {self.slug} 缺少实例")
        try:
            await _await_optional(getattr(self.instance, "initialize", None))
            self.state = AstrUnitState.ACTIVE
        except Exception as exc:  # noqa: BLE001
            self.state = AstrUnitState.QUARANTINED
            self.failure = f"initialize 失败: {type(exc).__name__}: {exc}"
            logger.error(f"AstrBot 单元隔离 {self.slug}: {self.failure}")

    async def drain_and_unload(self, timeout: float) -> None:
        """阻止新调用，取消受管任务，等待调用结束并执行终止钩子。"""
        if self.state is AstrUnitState.UNLOADED:
            return
        self.state = AstrUnitState.DRAINING
        for task_id in list(self.task_ids):
            get_task_manager().cancel_task(task_id)
        pending_tasks = [task for task in self._tasks.values() if not task.done()]

        async def wait_for_leases() -> None:
            """等待已进入外部单元的调用安全退出。"""
            async with self._lease_condition:
                if self.active_calls:
                    await self._lease_condition.wait_for(lambda: self.active_calls == 0)

        try:
            await asyncio.wait_for(
                asyncio.gather(
                    *pending_tasks, wait_for_leases(), return_exceptions=True
                ),
                timeout=max(timeout, 0.01),
            )
        except TimeoutError:
            logger.warning(f"AstrBot 单元 {self.slug} 受管任务取消超时，执行强制取消")
            for task in pending_tasks:
                task.cancel()
            if self.active_calls:
                logger.warning(
                    f"AstrBot 单元 {self.slug} lease drain 超时，仍有 {self.active_calls} 个调用"
                )
        if self.instance is not None:
            try:
                await asyncio.wait_for(
                    _await_optional(getattr(self.instance, "terminate", None)),
                    timeout=max(timeout, 0.01),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"AstrBot 单元 {self.slug} terminate 失败: {exc}")
        self.handlers.clear()
        self.hook_handlers = {hook: [] for hook in _SUPPORTED_HOOKS}
        self.llm_tool_handlers.clear()
        self._function_tools.clear()
        self.web_routes.clear()
        self.messages.clear()
        self.task_ids.clear()
        self._tasks.clear()
        self.instance = None
        self.context = None
        self._cleanup_modules()
        self.state = AstrUnitState.UNLOADED

    async def acquire_lease(self) -> None:
        """进入外部代码前获取调用租约。"""
        async with self._lease_condition:
            if self.state is not AstrUnitState.ACTIVE:
                raise UnitUnavailableError(
                    f"AstrBot 单元 {self.slug} 不可用: {self.state.value}"
                )
            self.active_calls += 1

    async def release_lease(self) -> None:
        """释放调用租约并唤醒等待卸载的任务。"""
        async with self._lease_condition:
            self.active_calls = max(0, self.active_calls - 1)
            if self.active_calls == 0:
                self._lease_condition.notify_all()

    async def dispatch(self, message: Message) -> bool:
        """在消息过滤器匹配时调用外部处理器。"""
        await self.acquire_lease()
        try:
            self.messages[message.stream_id] = message
            event = self._make_event(message)
            text = str(message.processed_plain_text or message.content or "")
            for handler in self.handlers:
                filters = self._handler_filters(handler)
                if not await self._matches(filters, text, message, event):
                    continue
                result = self._invoke_handler(handler, event, text)
                if inspect.isasyncgen(result):
                    async for item in result:
                        await self._consume_result(item, message)
                else:
                    item = await result if inspect.isawaitable(result) else result
                    await self._consume_result(item, message)
                if event.is_stopped():
                    return True
            return event.is_stopped()
        finally:
            await self.release_lease()

    async def invoke_llm_tool(
        self, handler: Callable[..., Any], **kwargs: Any
    ) -> tuple[bool, Any]:
        """执行预备期声明的外部 LLM 工具。"""
        await self.acquire_lease()
        try:
            event = self._latest_event()
            result = _invoke_tool_handler(handler, event, kwargs)
            if inspect.isawaitable(result):
                result = await result
            if isinstance(result, MessageEventResult):
                return True, result.text
            if isinstance(result, tuple):
                return bool(result[0]), result[1] if len(result) > 1 else ""
            return True, result
        except Exception as exc:  # noqa: BLE001
            logger.error(
                f"AstrBot 工具 {self.slug}.{getattr(handler, '__name__', '?')} 失败: {exc}"
            )
            return False, f"工具执行失败: {exc}"
        finally:
            await self.release_lease()

    def add_llm_tools(self, *tools: Any) -> None:
        """在预备期登记 FunctionTool，激活后严禁新增宿主工具。"""
        if self.state is not AstrUnitState.DISCOVERED:
            raise LateComponentRegistrationError(
                f"AstrBot 单元 {self.slug} 已越过组件预备期，不能新增 LLM 工具"
            )
        for tool in tools:
            if not isinstance(tool, FunctionTool):
                raise AstrUnitAdmissionError(
                    f"Context.add_llm_tools() 只接受 FunctionTool，收到 {type(tool).__name__}"
                )
            if not tool.name.strip():
                raise AstrUnitAdmissionError("FunctionTool 名称不能为空")
            self._function_tools.append(tool)
            self._llm_tools.add_tool(tool)

    def add_web_route(
        self,
        route: str,
        handler: Callable[..., Any],
        methods: list[str] | None,
        desc: str,
    ) -> None:
        """登记固定桥前缀下的 Web API。"""
        if self.state not in {
            AstrUnitState.DISCOVERED,
            AstrUnitState.PREPARED,
            AstrUnitState.REGISTERED,
            AstrUnitState.ACTIVE,
        }:
            raise UnitUnavailableError(f"单元 {self.slug} 当前不能登记 Web API")
        normalized_route = str(route or "").strip()
        if (
            not normalized_route
            or normalized_route.startswith("/")
            or ".." in normalized_route.split("/")
        ):
            raise AstrUnitAdmissionError("Web API route 必须是无 '..' 的相对路径")
        normalized_methods = tuple(str(item).upper() for item in (methods or ["GET"]))
        if not normalized_methods or any(
            item not in _SUPPORTED_WEB_METHODS for item in normalized_methods
        ):
            raise AstrUnitAdmissionError("Web API 包含不支持的 HTTP 方法")
        if not callable(handler):
            raise AstrUnitAdmissionError("Web API handler 必须可调用")
        if any(
            route_item.route == normalized_route
            and set(route_item.methods) & set(normalized_methods)
            for route_item in self.web_routes
        ):
            raise AstrUnitAdmissionError(f"Web API 路由冲突: {normalized_route}")
        self.web_routes.append(
            AstrWebRoute(self, normalized_route, handler, normalized_methods, desc)
        )

    def register_task(self, coro: Coroutine[Any, Any, Any]) -> None:
        """通过 TaskManager 创建当前单元拥有的后台任务。"""
        if self.state is not AstrUnitState.ACTIVE:
            raise UnitUnavailableError(f"单元 {self.slug} 未激活，不能创建后台任务")
        task_info = get_task_manager().create_task(
            coro,
            name=f"compat:astr:{self.slug}",
            group_name=f"compat:astr:{self.slug}",
            metadata={"bridge": "astr", "slug": self.slug},
        )
        self.task_ids.add(task_info.task_id)
        if task_info.task is not None:
            task_id = task_info.task_id
            self._tasks[task_id] = task_info.task
            task_info.task.add_done_callback(lambda _: self._discard_task(task_id))

    def _discard_task(self, task_id: str) -> None:
        """从当前单元的受管任务索引中移除已结束任务。"""
        self.task_ids.discard(task_id)
        self._tasks.pop(task_id, None)

    def _load_config(self, plugin_dir: Path) -> None:
        """从外部 schema 提取默认值，并保持 AstrBotConfig 类型。"""
        schema_file = plugin_dir / "_conf_schema.json"
        if not schema_file.is_file():
            self.config_dict = AstrBotConfig()
            return
        try:
            schema = json.loads(schema_file.read_text(encoding="utf-8"))
            self.config_dict = AstrBotConfig(
                _schema_defaults(schema if isinstance(schema, dict) else {})
            )
        except (OSError, ValueError, TypeError) as exc:
            raise AstrUnitAdmissionError(f"解析 _conf_schema.json 失败: {exc}") from exc

    def _import_module(self, main_file: Path) -> types.ModuleType:
        """用桥私有包前缀导入外部 main.py 并支持相对导入。"""
        package = types.ModuleType(self.module_prefix)
        package.__path__ = [str(main_file.parent)]
        package.__package__ = self.module_prefix
        sys.modules[self.module_prefix] = package
        module_name = f"{self.module_prefix}.main"
        spec = importlib.util.spec_from_file_location(
            module_name,
            main_file,
            submodule_search_locations=[str(main_file.parent)],
        )
        if spec is None or spec.loader is None:
            raise AstrUnitAdmissionError(f"无法创建外部模块规格: {main_file}")
        module = importlib.util.module_from_spec(spec)
        module.__package__ = self.module_prefix
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module

    @staticmethod
    def _find_star_class(module: types.ModuleType) -> type[Star]:
        """找出模块中唯一一个非基类 Star 子类。"""
        sdk_star = getattr(sys.modules.get("astrbot.api.star"), "Star", Star)
        candidates = [
            value
            for value in vars(module).values()
            if inspect.isclass(value)
            and issubclass(value, sdk_star)
            and value is not sdk_star
        ]
        if len(candidates) != 1:
            raise AstrUnitAdmissionError("AstrBot 插件必须定义唯一的 Star 子类")
        return candidates[0]

    def _instantiate_star(self, star_cls: type[Star]) -> Star:
        """按构造函数形态实例化外部 Star。"""
        assert self.context is not None
        signature = inspect.signature(star_cls.__init__)
        params = [
            parameter
            for parameter in signature.parameters.values()
            if parameter.name != "self"
        ]
        if len(params) >= 2:
            return star_cls(self.context, self.config_dict)
        return star_cls(self.context)

    def _capture_message_component_types(self) -> None:
        """登记外部单元实际导入的 Astr 消息组件类。

        兼容 SDK 是进程级导入面；其他隔离加载器刷新 ``astrbot.*`` 后，已经
        导入的单元仍可能持有旧组件类。本方法在外部模块加载完成后递归检查其
        私有前缀模块，并只登记与当前 SDK 组件契约相符的已知类。
        """
        component_names = ("Plain", "At", "Image", "Record", "Reply", "Video", "File")
        canonical_module = sys.modules.get("astrbot.api.message_components")
        canonical_types: dict[str, type[Any]] = {}
        if canonical_module is not None:
            for name in component_names:
                component_type = getattr(canonical_module, name, None)
                if isinstance(component_type, type):
                    canonical_types[name] = component_type
        candidates: dict[str, list[type[Any]]] = {name: [] for name in component_names}
        for module_name, module in tuple(sys.modules.items()):
            if module_name != self.module_prefix and not module_name.startswith(
                f"{self.module_prefix}."
            ):
                continue
            for name in component_names:
                component_type = getattr(module, name, None)
                if isinstance(component_type, type):
                    _append_component_candidate(candidates[name], component_type)
            for value in vars(module).values():
                if not isinstance(value, type):
                    continue
                component_name = _component_name_for_type(value)
                if component_name is not None:
                    _append_component_candidate(candidates[component_name], value)
            component_namespace = getattr(module, "Comp", None)
            for name in component_names:
                component_type = getattr(component_namespace, name, None)
                if isinstance(component_type, type):
                    _append_component_candidate(candidates[name], component_type)
        compatible_candidates = {
            name: tuple(
                component_type
                for component_type in component_types
                if _is_compatible_component_type(name, component_type)
            )
            for name, component_types in candidates.items()
        }
        unregister_compatible_component_types(self._component_type_registrations)
        registrations: list[tuple[str, type[Any]]] = []
        for name, component_types in compatible_candidates.items():
            for component_type in component_types:
                registrations.extend(
                    register_compatible_component_types({name: component_type})
                )
        self._component_type_registrations = tuple(registrations)
        self._message_component_types = {
            name: _make_component_identity_type(
                name,
                canonical_types.get(name),
                compatible_candidates.get(name, ()),
                self.module_prefix,
            )
            for name in component_names
            if canonical_types.get(name) is not None or compatible_candidates.get(name)
        }

    def get_message_component_type(self, name: str, fallback: type[Any]) -> type[Any]:
        """返回当前外部单元使用的兼容消息组件类型。"""
        return self._message_component_types.get(name, fallback)

    def _classify_declarations(self) -> None:
        """将外部处理器、钩子和工具按元数据分类。"""
        if self.instance is None:
            raise AstrUnitAdmissionError("Star 实例未创建")
        sdk_filter = sys.modules.get("astrbot.api.event.filter")
        registry_type = getattr(sdk_filter, "_FilterRegistry", _FilterRegistry)
        self.handlers.clear()
        self.llm_tool_handlers.clear()
        self.hook_handlers = {hook: [] for hook in _SUPPORTED_HOOKS}
        for _, method in inspect.getmembers(self.instance, predicate=inspect.ismethod):
            filters = getattr(method, "__mofox_astr_filters__", None)
            if not isinstance(filters, registry_type) or not filters:
                continue
            kinds = {entry.kind for entry in filters}
            rejected = kinds & _REJECTED_HOOKS
            if rejected:
                raise AstrUnitAdmissionError(
                    f"声明了未映射 AstrBot 钩子: {', '.join(sorted(rejected))}"
                )
            if kinds == {"command_group"}:
                continue
            if "llm_tool" in kinds:
                entry = next(item for item in filters if item.kind == "llm_tool")
                name = str(entry.value.get("name", "") or method.__name__).strip()
                self.llm_tool_handlers.append((name, method))
                continue
            supported_hooks = kinds & _SUPPORTED_HOOKS
            if supported_hooks:
                for hook in supported_hooks:
                    self.hook_handlers[hook].append(method)
                continue
            if kinds & {
                "command",
                "command_group",
                "regex",
                "event_message_type",
                "platform_adapter_type",
                "permission_type",
                "custom_filter",
            }:
                self.handlers.append(method)

    def _validate_admission(self) -> None:
        """拒绝无法由桥完整映射的声明。"""
        if self.instance is None:
            raise AstrUnitAdmissionError("Star 实例未创建")
        sdk_filter = sys.modules.get("astrbot.api.event.filter")
        registry_type = getattr(sdk_filter, "_FilterRegistry", _FilterRegistry)
        for method in self.handlers:
            filters = getattr(method, "__mofox_astr_filters__", registry_type())
            for entry in filters:
                if entry.kind == "regex" and not hasattr(entry.value, "search"):
                    raise AstrUnitAdmissionError(
                        f"正则过滤器不可编译: {method.__name__}"
                    )
                if (
                    entry.kind == "platform_adapter_type"
                    and not _is_onebot_adapter_filter(entry.value)
                ):
                    raise AstrUnitAdmissionError("仅支持 AIOCQHTTP 平台过滤器")
                if (
                    entry.kind == "permission_type"
                    and not _is_supported_permission_filter(entry.value)
                ):
                    raise AstrUnitAdmissionError("仅支持 ADMIN 或 MEMBER 权限过滤器")
        for name, handler in self.llm_tool_handlers:
            if not name:
                raise AstrUnitAdmissionError(
                    f"LLM 工具名称不能为空: {handler.__name__}"
                )
        unsupported_methods = (
            "register_provider",
            "register_platform",
            "get_platform_inst",
        )
        for method_name in unsupported_methods:
            if method_name in type(self.instance).__dict__:
                raise AstrUnitAdmissionError(f"不支持的 AstrBot 声明: {method_name}")

    def _build_dynamic_tools(self) -> None:
        """为外部工具生成命名空间隔离的 BaseTool 类型。"""
        seen: set[str] = set()
        self.dynamic_tools.clear()
        for external_name, handler in self.llm_tool_handlers:
            namespaced = _namespaced_tool_name(self.slug, external_name)
            if namespaced in seen:
                raise AstrUnitAdmissionError(f"重复 LLM 工具: {external_name}")
            seen.add(namespaced)
            self.dynamic_tools.append(
                _make_handler_tool_class(self, namespaced, external_name, handler)
            )
        for tool in self._function_tools:
            namespaced = _namespaced_tool_name(self.slug, tool.name)
            if namespaced in seen:
                raise AstrUnitAdmissionError(f"重复 FunctionTool: {tool.name}")
            seen.add(namespaced)
            self.dynamic_tools.append(_make_function_tool_class(self, namespaced, tool))

    def _make_event(self, message: Message) -> AstrMessageEvent:
        """针对 QQ profile 创建 OneBot 事件，其他平台创建通用事件。

        同步推断 AstrBot 唤醒状态并写入 ``message.extra["is_at_or_wake_command"]``，
        使外部插件通过 ``event.is_at_or_wake_command`` 读到与 AstrBot 一致的语义：
        私聊默认唤醒；群聊在 @机器人、回复机器人或以 wake_prefix（默认 ``/``）
        开头时唤醒。
        """
        _infer_wake_command(message)
        if str(message.platform or "").lower() == "qq":
            return AiocqhttpMessageEvent(
                self.bridge, message, message.stream_id, unit=self
            )
        return AstrMessageEvent(self.bridge, message, message.stream_id, unit=self)

    def _latest_event(self) -> AstrMessageEvent:
        """为工具调用构造最新来源消息事件。"""
        if not self.messages:
            raise UnitUnavailableError(f"AstrBot 工具 {self.slug} 没有可用的来源消息")
        source = next(reversed(self.messages.values()))
        return self._make_event(source)

    async def _matches(
        self,
        filters: _FilterRegistry,
        text: str,
        message: Message,
        event: AstrMessageEvent,
    ) -> bool:
        """严格判断全部 AstrBot 过滤器。"""
        is_wake = bool(event.is_at_or_wake_command)
        for entry in filters:
            if entry.kind == "command" and not _match_command(entry, text, is_wake):
                return False
            if entry.kind == "command_group":
                continue
            if entry.kind == "regex" and entry.value.search(text) is None:
                return False
            if entry.kind == "event_message_type" and not _matches_message_type(
                entry.value, message
            ):
                return False
            if entry.kind == "platform_adapter_type" and not _matches_platform(
                entry.value, message
            ):
                return False
            if entry.kind == "permission_type" and not await _matches_permission(
                entry.value, message
            ):
                return False
            if entry.kind == "custom_filter":
                filter_obj = entry.value
                try:
                    result = (
                        filter_obj.filter(event, self.config_dict)
                        if hasattr(filter_obj, "filter")
                        else filter_obj(event, self.config_dict)
                    )
                    if inspect.isawaitable(result):
                        result = await result
                    if not bool(result):
                        return False
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"AstrBot custom_filter 失败 ({self.slug}): {exc}")
                    return False
        return True

    def _invoke_handler(
        self, handler: Callable[..., Any], event: AstrMessageEvent, text: str
    ) -> Any:
        """解析命令剩余参数后调用处理器。"""
        filters = self._handler_filters(handler)
        command_filter = next(
            (item for item in filters if item.kind == "command"), None
        )
        if command_filter is not None:
            rest = _command_rest(
                command_filter, text, bool(event.is_at_or_wake_command)
            )
        else:
            rest = ""
        parameters = [
            parameter
            for parameter in inspect.signature(handler).parameters.values()
            if parameter.name not in {"self", "event"}
        ]
        tokens = rest.split()
        args: list[Any] = []
        keyword_args: dict[str, Any] = {}
        for parameter in parameters:
            if parameter.kind == inspect.Parameter.VAR_POSITIONAL:
                args.extend(_convert_token(token, None) for token in tokens)
                tokens.clear()
                break
            if parameter.kind in {
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            }:
                if tokens:
                    args.append(_convert_token(tokens.pop(0), parameter.annotation))
                elif parameter.default is inspect.Parameter.empty:
                    return None
            if parameter.kind == inspect.Parameter.KEYWORD_ONLY:
                if tokens:
                    keyword_args[parameter.name] = _convert_token(
                        tokens.pop(0), parameter.annotation
                    )
                elif parameter.default is inspect.Parameter.empty:
                    return None
        return handler(event, *args, **keyword_args)

    @staticmethod
    def _handler_filters(handler: Callable[..., Any]) -> Any:
        """读取当前已安装 Astr shim 生成的过滤器注册表。"""
        sdk_filter = sys.modules.get("astrbot.api.event.filter")
        registry_type = getattr(sdk_filter, "_FilterRegistry", _FilterRegistry)
        return getattr(handler, "__mofox_astr_filters__", registry_type())

    async def _consume_result(self, item: Any, source: Message) -> None:
        """发送外部处理器产生的结果链。"""
        if isinstance(item, MessageEventResult):
            await self.bridge.send_result(item, source, unit=self)
        elif isinstance(item, str) and item:
            await self.bridge.send_result(
                MessageEventResult.plain(item), source, unit=self
            )

    def _cleanup_modules(self) -> None:
        """卸载当前单元导入过的桥私有模块。"""
        unregister_compatible_component_types(self._component_type_registrations)
        self._component_type_registrations = ()
        self._message_component_types.clear()
        for module_name in list(sys.modules):
            if module_name == self.module_prefix or module_name.startswith(
                f"{self.module_prefix}."
            ):
                sys.modules.pop(module_name, None)
        self.module = None


class AstrCompatRouter(BaseRouter):
    """静态注册、运行时按单元路由表分派的 Astr Web API Router。"""

    name = "astr_compat_router"
    description = "AstrBot 兼容桥受限 Web API"
    custom_route_path = "/compat/astr"

    def register_endpoints(self) -> None:
        """注册 catch-all 端点并由桥进行路径、方法和状态校验。"""

        @self.app.api_route(
            "/{slug}/{relative_path:path}", methods=list(_SUPPORTED_WEB_METHODS)
        )
        async def dispatch(slug: str, relative_path: str, request: Request) -> Any:
            bridge = getattr(self.plugin, "bridge", None)
            if not isinstance(bridge, AstrPluginBridge):
                raise HTTPException(status_code=503, detail="Astr 兼容桥不可用")
            return await bridge.dispatch_web_request(slug, relative_path, request)


class AstrPluginBridge(BaseEventHandler):
    """管理外部 Astr 单元、OneBot 服务、事件分派和桥 Router。"""

    name = "astr_plugin_bridge"
    description = "AstrBot Star 插件消息事件桥"
    # 强依赖 onebot_expand 的两个 Service：发送链路与账号/群资料查询。
    dependencies = [_MESSAGE_SERVICE_SIGNATURE, _ACCOUNT_SERVICE_SIGNATURE]
    init_subscribe = [
        EventType.ON_MESSAGE_RECEIVED,
        EventType.ON_NOTICE_RECEIVED,
        EventType.ON_ALL_PLUGIN_LOADED,
        EventType.AFTER_MESSAGE_SENT,
        EventType.BEFORE_LLM_REQUEST,
        EventType.AFTER_LLM_REQUEST,
    ]
    timeout = 0

    def __init__(self, plugin: BasePlugin) -> None:
        """初始化桥共享状态。"""
        super().__init__(plugin)
        self.units: list[AstrPluginUnit] = []
        self._units_by_slug: dict[str, AstrPluginUnit] = {}
        self._web_routes: dict[tuple[str, str, str], AstrWebRoute] = {}
        self._provider_tasks: dict[str, str] = {}
        self.provider_manager = _ProviderManager(self)
        self._router_signature = build_signature(
            plugin.plugin_name,
            ComponentType.ROUTER,
            AstrCompatRouter.name,
        )
        self._router_mounted = False
        self._provider_tasks = self._read_provider_task_map()
        self._config_poller: ConfigChangePoller | None = None
        self._plugin_slug_field_map: dict[str, dict[str, str]] = {}

    def _shared_bridge(self) -> "AstrPluginBridge":
        """返回插件构造期持有的共享 bridge，而非宿主临时组件实例。"""
        runtime = getattr(self.plugin, "bridge", None)
        return runtime if isinstance(runtime, AstrPluginBridge) else self

    @property
    def llm_tool_manager(self) -> ToolSet:
        """合并并返回所有预备期外部 FunctionTool。"""
        merged = ToolSet()
        for unit in self.units:
            for tool in unit._llm_tools.get_all_tools():
                merged.add_tool(tool)
        return merged

    def prepare_units(self) -> None:
        """在插件构造期同步准备全部外部单元和动态工具。"""
        config = self.plugin.config
        if not isinstance(config, AstrCompatConfig) or not config.plugin.enabled:
            return
        self._assert_onebot_services_declared()
        for directory in self._resolve_plugin_dirs(config):
            slug = _make_slug(directory)
            if slug in self._units_by_slug:
                logger.error(f"AstrBot 单元 slug 冲突: {slug}")
                continue
            unit = AstrPluginUnit(self, directory, slug)
            try:
                unit.prepare()
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    f"AstrBot 单元准入拒绝 {directory}: {type(exc).__name__}: {exc}"
                )
                continue
            self.units.append(unit)
            self._units_by_slug[slug] = unit
        self._rebuild_web_routes()
        self._install_merged_config()

    def _install_merged_config(self) -> None:
        """构建合并配置类并替换 ConfigManager 缓存，使 webui 能编辑被兼容插件配置。

        被兼容 AstrBot 插件的 ``_conf_schema.json`` 被转换为 SectionBase 子类，
        合并到 ``AstrCompatConfig`` 的子类中。替换 ConfigManager 缓存后，webui
        的 ConfigParser 会从合并配置类的 model_fields 提取全部 section schema。
        同时启动配置文件变更轮询，在 webui 保存后把新配置分发回插件实例。
        """
        if not self.units:
            return
        plugin_sections, slug_field_map = collect_plugin_sections(self.units)
        self._plugin_slug_field_map = slug_field_map
        if not plugin_sections:
            logger.debug("无被兼容插件配置需要合并")
            return
        merged_cls = build_merged_config_class(
            AstrCompatConfig,
            plugin_sections,
            "MergedAstrCompatConfig",
        )
        config_path = (
            Path("config") / "plugins" / self.plugin.plugin_name / "config.toml"
        )
        merged_instance = install_merged_config(
            self.plugin.plugin_name, merged_cls, config_path
        )
        # 同步桥自身持有的配置引用，使后续读取到合并后的值
        self.plugin.config = merged_instance
        logger.info(
            f"AstrBot 桥合并配置已安装，含 {len(plugin_sections)} 个被兼容插件配置"
        )
        self._config_poller = ConfigChangePoller(
            config_path,
            on_change=self._on_config_file_changed,
        )
        self._config_poller.start()
        logger.info("AstrBot 桥配置轮询已启动")

    def _on_config_file_changed(self, data: dict[str, Any]) -> None:
        """webui 保存配置后，把被兼容插件部分分发回各单元实例。"""
        apply_toml_data_to_units(data, self.units, self._plugin_slug_field_map)
        logger.debug("AstrBot 桥配置变更已分发到被兼容插件实例")

    def mark_registered(self) -> None:
        """在宿主获得组件快照后冻结外部宿主组件集合。"""
        for unit in self.units:
            if unit.state is AstrUnitState.PREPARED:
                unit.state = AstrUnitState.REGISTERED

    async def activate(self) -> None:
        """激活所有已注册单元并挂载静态 Router。"""
        for unit in self.units:
            if unit.state is AstrUnitState.REGISTERED:
                await unit.activate()
        self._router_mounted = (
            router_api.get_mounted_router(self._router_signature) is not None
        )

    async def activate_shared(self) -> None:
        """从已注册的事件组件安全委派到构造期共享 bridge。"""
        runtime = self._shared_bridge()
        if runtime is not self:
            await runtime.activate()
            return
        await self.activate()

    async def unload(self, timeout: float = 5.0) -> None:
        """按单元执行 drain、终止、服务表清理和 Router 卸载。"""
        config = self.plugin.config
        section = getattr(config, "plugin", None)
        configured_timeout = getattr(section, "unload_timeout_seconds", timeout)
        if isinstance(configured_timeout, (int, float)) and configured_timeout > 0:
            timeout = float(configured_timeout)
        if self._config_poller is not None:
            await self._config_poller.stop()
            self._config_poller = None
        self._web_routes.clear()
        for unit in self.units:
            await unit.drain_and_unload(timeout)
        self.units.clear()
        self._units_by_slug.clear()
        self._plugin_slug_field_map.clear()
        if router_api.get_mounted_router(self._router_signature) is not None:
            await router_api.unmount_router(self._router_signature)
        self._router_mounted = False

    async def unload_shared(self, timeout: float = 5.0) -> None:
        """从已注册的事件组件安全委派到构造期共享 bridge 卸载。"""
        runtime = self._shared_bridge()
        if runtime is not self:
            await runtime.unload(timeout)
            return
        await self.unload(timeout)

    async def execute(
        self, event_name: str, params: dict[str, Any]
    ) -> tuple[EventDecision, dict[str, Any]]:
        """把宿主事件路由给活动外部单元，始终保持参数键集合。"""
        runtime = getattr(self.plugin, "bridge", None)
        if isinstance(runtime, AstrPluginBridge) and runtime is not self:
            return await runtime._execute_shared(event_name, params)
        return await self._execute_shared(event_name, params)

    async def _execute_shared(
        self, event_name: str, params: dict[str, Any]
    ) -> tuple[EventDecision, dict[str, Any]]:
        """执行持有预备单元状态的共享事件派发逻辑。"""
        if event_name == EventType.ON_MESSAGE_RECEIVED.value:
            return await self._dispatch_message(params)
        if event_name == EventType.ON_NOTICE_RECEIVED.value:
            return await self._dispatch_message(params)
        if event_name == EventType.ON_ALL_PLUGIN_LOADED.value:
            for unit in self._active_units():
                await self._run_simple_hooks(unit, "on_astrbot_loaded")
            return EventDecision.SUCCESS, params
        if event_name == EventType.AFTER_MESSAGE_SENT.value:
            for unit in self._active_units():
                await self._run_simple_hooks(unit, "after_message_sent")
            return EventDecision.SUCCESS, params
        if event_name == EventType.BEFORE_LLM_REQUEST.value:
            await self._run_llm_request_hooks(params)
            return EventDecision.SUCCESS, params
        if event_name == EventType.AFTER_LLM_REQUEST.value:
            await self._run_llm_response_hooks(params)
            return EventDecision.SUCCESS, params
        return EventDecision.PASS, params

    async def send_result(
        self, result: MessageEventResult, source: Message, unit: AstrPluginUnit | None
    ) -> bool:
        """把完整处理结果转换为 OneBot 原始段后按来源会话发送。"""
        if unit is not None and unit.state is not AstrUnitState.ACTIVE:
            raise UnitUnavailableError(f"AstrBot 单元 {unit.slug} 不可发送消息")
        segments = result.to_segments()
        return await self._send_segments(source, segments)

    async def send_to_umo(
        self,
        umo: str,
        *,
        segments: list[dict[str, Any]],
        unit: AstrPluginUnit | None,
    ) -> bool:
        """从所属单元的消息快照中定位来源会话并发送完整段列表。"""
        source = unit.messages.get(umo) if unit is not None else None
        if source is None:
            for candidate in self.units:
                source = candidate.messages.get(umo)
                if source is not None:
                    break
        if source is None:
            raise UnitUnavailableError(f"无法找到 AstrBot 主动发送的来源会话: {umo}")
        return await self._send_segments(source, segments)

    def register_web_api(
        self,
        route: str,
        handler: Callable[..., Any],
        methods: list[str] | None,
        desc: str,
        *,
        unit: AstrPluginUnit | None,
    ) -> None:
        """登记当前单元 Web API 并即时更新静态 Router 的分发表。"""
        if unit is None:
            raise AstrUnitAdmissionError("Web API 必须绑定外部 AstrBot 单元")
        unit.add_web_route(route, handler, methods, desc)
        try:
            self._rebuild_web_routes()
        except Exception:
            unit.web_routes.pop()
            raise

    def add_llm_tools(self, *tools: Any, unit: AstrPluginUnit | None) -> None:
        """将 FunctionTool 绑定到调用它的预备期单元。"""
        if unit is None:
            raise AstrUnitAdmissionError("LLM 工具必须绑定外部 AstrBot 单元")
        unit.add_llm_tools(*tools)

    def register_task(
        self, coro: Coroutine[Any, Any, Any], *, unit: AstrPluginUnit | None
    ) -> None:
        """委派当前单元创建受管后台任务。"""
        if unit is None:
            raise AstrUnitAdmissionError("后台任务必须绑定外部 AstrBot 单元")
        unit.register_task(coro)

    def get_config_for_unit(self, unit: AstrPluginUnit | None) -> dict[str, Any]:
        """返回不暴露可写共享状态的配置副本。"""
        config = dict(unit.config_dict) if unit is not None else {}
        config.setdefault("admins_id", [])
        return config

    def get_llm_tool_manager(self, unit: AstrPluginUnit | None) -> ToolSet:
        """返回单元预备期登记的工具集合。"""
        if unit is None:
            raise AstrUnitAdmissionError("LLM 工具管理器必须绑定单元")
        return unit._llm_tools

    def activate_llm_tool(self, name: str, unit: AstrPluginUnit | None) -> bool:
        """验证工具存在；工具已在宿主快照中注册，不维护伪激活状态。"""
        if unit is None or unit._llm_tools.get_tool(name) is None:
            raise UnsupportedAstrCapabilityError(f"未注册 AstrBot LLM 工具: {name}")
        return True

    def deactivate_llm_tool(self, name: str, unit: AstrPluginUnit | None) -> bool:
        """拒绝运行时移除宿主已快照的工具。"""
        del name, unit
        raise LateComponentRegistrationError(
            "Neo-MoFox 不支持运行时移除已快照的 AstrBot 工具"
        )

    def get_registered_star(self, star_name: str) -> Star | None:
        """按类名或注册名查找同桥外部 Star。"""
        normalized = str(star_name or "").strip()
        for unit in self.units:
            instance = unit.instance
            if instance is not None and normalized in {
                type(instance).__name__,
                getattr(instance, "name", ""),
            }:
                return instance
        return None

    def get_all_stars(self) -> list[Star]:
        """返回当前仍持有实例的外部 Star。"""
        return [unit.instance for unit in self.units if unit.instance is not None]

    def has_provider(self, provider_id: str) -> bool:
        """判断 Provider ID 是否有显式宿主模型任务映射。"""
        return str(provider_id or "").strip() in self._provider_tasks

    def get_using_provider(self) -> Provider:
        """返回唯一默认 Provider；没有映射时明确失败。"""
        if not self._provider_tasks:
            raise UnknownProviderIdError(
                "Astr 兼容桥未配置任何 Provider ID 到模型任务映射"
            )
        return Provider(self, next(iter(self._provider_tasks)))

    def get_provider_by_id(self, provider_id: str) -> Provider:
        """返回指定 Provider 或抛出稳定未知 ID 错误。"""
        if not self.has_provider(provider_id):
            raise UnknownProviderIdError(f"未配置 AstrBot Provider ID: {provider_id!r}")
        return Provider(self, provider_id)

    def get_all_providers(self) -> list[Provider]:
        """返回所有已配置 Provider。"""
        return [Provider(self, provider_id) for provider_id in self._provider_tasks]

    async def llm_text_chat(
        self,
        *,
        provider_id: str,
        prompt: str,
        image_urls: list[str],
        audio_urls: list[str],
        tools: Any = None,
        contexts: list[dict[str, Any]] | None = None,
        system_prompt: str = "",
        unit: AstrPluginUnit | None = None,
        **kwargs: Any,
    ) -> CompatLLMResponse:
        """以显式 Provider 模型任务构建真实宿主 LLMRequest。"""
        del kwargs
        if not self.has_provider(provider_id):
            raise UnknownProviderIdError(f"未配置 AstrBot Provider ID: {provider_id!r}")
        if image_urls or audio_urls:
            raise UnsupportedAstrCapabilityError(
                "当前 Astr LLM 映射不支持 image_urls 或 audio_urls"
            )
        if tools not in (None, [], ()):
            raise UnsupportedAstrCapabilityError("当前 Astr LLM 映射不支持临时工具参数")
        if unit is not None:
            await unit.acquire_lease()
        try:
            payloads = _build_llm_payloads(system_prompt, contexts or [], prompt)
            request = llm_api.create_llm_request(
                llm_api.get_model_set_by_task(self._provider_tasks[provider_id]),
                request_name=f"compat.astr.{provider_id}",
            )
            for payload in payloads:
                request.add_payload(payload)
            response = await request.send(stream=False)
            return CompatLLMResponse(
                content=response.message or "",
                role="assistant",
                tool_calls=[
                    {"id": call.id, "name": call.name, "args": call.args}
                    for call in response.call_list or []
                ],
                reasoning_content=response.reasoning_content,
                reasoning_parts=response.reasoning_parts,
                raw=response,
            )
        finally:
            if unit is not None:
                await unit.release_lease()

    async def onebot_get_stranger_info(
        self, message: Message, user_id: int | str, no_cache: bool
    ) -> dict[str, Any]:
        """经 AccountService 查询陌生人资料。"""
        service = self._account_service(message)
        return self._validate_onebot_response(
            await service.get_stranger_info(
                _as_positive_int(user_id, "user_id"), bool(no_cache)
            )
        )

    async def onebot_get_group_member_info(
        self,
        message: Message,
        group_id: int | str,
        user_id: int | str,
        no_cache: bool,
    ) -> dict[str, Any]:
        """经 AccountService 查询群成员资料。"""
        service = self._account_service(message)
        return self._validate_onebot_response(
            await service.get_group_member_info(
                _as_positive_int(group_id, "group_id"),
                _as_positive_int(user_id, "user_id"),
                bool(no_cache),
            )
        )

    async def onebot_get_group_member_list(
        self,
        message: Message,
        group_id: int | str,
        no_cache: bool,
    ) -> dict[str, Any]:
        """经 AccountService 查询群成员列表。"""
        service = self._account_service(message)
        return self._validate_onebot_response(
            await service.get_group_member_list(
                _as_positive_int(group_id, "group_id"),
                bool(no_cache),
            )
        )

    async def onebot_get_group_info(
        self,
        message: Message,
        group_id: int | str,
        no_cache: bool,
    ) -> dict[str, Any]:
        """经 AccountService 查询群资料。"""
        service = self._account_service(message)
        return self._validate_onebot_response(
            await service.get_group_info(
                _as_positive_int(group_id, "group_id"),
                bool(no_cache),
            )
        )

    async def onebot_send_group_msg(
        self,
        message: Message,
        group_id: int | str,
        segments: list[dict[str, Any]],
        auto_escape: bool,
    ) -> dict[str, Any]:
        """经 MessageService 发送群消息。"""
        service = self._message_service(message)
        return self._validate_onebot_response(
            await service.send_group_msg(
                _as_positive_int(group_id, "group_id"),
                _validate_onebot_segments(segments),
                bool(auto_escape),
            )
        )

    async def onebot_send_private_msg(
        self,
        message: Message,
        user_id: int | str,
        segments: list[dict[str, Any]],
        auto_escape: bool,
    ) -> dict[str, Any]:
        """经 MessageService 发送私聊消息。"""
        service = self._message_service(message)
        return self._validate_onebot_response(
            await service.send_private_msg(
                _as_positive_int(user_id, "user_id"),
                _validate_onebot_segments(segments),
                bool(auto_escape),
            )
        )

    async def onebot_delete_msg(
        self, message: Message, message_id: int | str
    ) -> dict[str, Any]:
        """经 MessageService 撤回消息。"""
        service = self._message_service(message)
        return self._validate_onebot_response(
            await service.delete_msg(_as_message_id(message_id))
        )

    async def onebot_get_msg(
        self, message: Message, message_id: int | str
    ) -> dict[str, Any]:
        """经 MessageService 查询消息。"""
        service = self._message_service(message)
        return self._validate_onebot_response(
            await service.get_msg(_as_message_id(message_id))
        )

    async def onebot_get_forward_msg(
        self, message: Message, message_id: int | str
    ) -> dict[str, Any]:
        """经 MessageService 查询合并转发。"""
        service = self._message_service(message)
        return self._validate_onebot_response(
            await service.get_forward_msg(str(message_id))
        )

    async def dispatch_web_request(
        self, slug: str, relative_path: str, request: Request
    ) -> Any:
        """执行与请求方法、路径严格匹配的外部 Web API。"""
        unit = self._units_by_slug.get(slug)
        if unit is None:
            raise HTTPException(status_code=404, detail="未知 AstrBot 单元")
        if unit.state in {AstrUnitState.DRAINING, AstrUnitState.UNLOADED}:
            raise HTTPException(status_code=503, detail="AstrBot 单元正在卸载")
        route = self._web_routes.get(
            (slug, str(request.method).upper(), relative_path.strip("/"))
        )
        if route is None:
            raise HTTPException(status_code=404, detail="未注册的 AstrBot Web API")
        await unit.acquire_lease()
        try:
            result = route.handler(request)
            return await result if inspect.isawaitable(result) else result
        finally:
            await unit.release_lease()

    async def _dispatch_message(
        self, params: dict[str, Any]
    ) -> tuple[EventDecision, dict[str, Any]]:
        """广播单条宿主消息，并按外部 stop_event 聚合阻断结果。"""
        message = params.get("message")
        if not isinstance(message, Message):
            return EventDecision.PASS, params
        stopped = False
        for unit in self._active_units():
            try:
                stopped = await unit.dispatch(message) or stopped
            except UnitUnavailableError:
                continue
            except Exception as exc:  # noqa: BLE001
                logger.error(f"AstrBot 单元消息派发失败 {unit.slug}: {exc}")
        return (EventDecision.STOP if stopped else EventDecision.SUCCESS, params)

    async def _run_simple_hooks(self, unit: AstrPluginUnit, kind: str) -> None:
        """执行无参数或仅事件可选参数的只读钩子。"""
        for handler in unit.hook_handlers.get(kind, []):
            await unit.acquire_lease()
            try:
                result = handler()
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"AstrBot 钩子 {unit.slug}.{kind} 失败: {exc}")
            finally:
                await unit.release_lease()

    async def _run_llm_request_hooks(self, params: dict[str, Any]) -> None:
        """将真实 Before LLM 参数映射为 ProviderRequest 并回写可变字段。"""
        request = ProviderRequest(
            prompt=_payload_text(params.get("payloads")),
            contexts=[],
            func_tool=None,
            payloads=list(params.get("payloads", [])),
            tools=list(params.get("tools", [])),
            stream=bool(params.get("stream", False)),
            meta_data=dict(params.get("meta_data", {})),
        )
        for unit in self._active_units():
            for handler in unit.hook_handlers.get("on_llm_request", []):
                await unit.acquire_lease()
                try:
                    result = _call_hook(handler, event=None, req=request)
                    if inspect.isawaitable(result):
                        await result
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"AstrBot LLM 请求钩子失败 {unit.slug}: {exc}")
                finally:
                    await unit.release_lease()
        if "payloads" in params and isinstance(request.payloads, list):
            params["payloads"] = request.payloads
        if "tools" in params and isinstance(request.tools, list):
            params["tools"] = request.tools
        if "stream" in params and isinstance(request.stream, bool):
            params["stream"] = request.stream

    async def _run_llm_response_hooks(self, params: dict[str, Any]) -> None:
        """将真实 After LLM 参数映射为 CompatLLMResponse 并回写支持字段。"""
        response = CompatLLMResponse(
            content=str(params.get("message") or ""),
            role="assistant",
            tool_calls=[
                {
                    "id": getattr(call, "id", None),
                    "name": getattr(call, "name", ""),
                    "args": getattr(call, "args", {}),
                }
                for call in params.get("tool_calls", [])
            ],
            reasoning_content=params.get("reasoning_content"),
            reasoning_parts=params.get("reasoning_parts"),
        )
        for unit in self._active_units():
            for handler in unit.hook_handlers.get("on_llm_response", []):
                await unit.acquire_lease()
                try:
                    result = _call_hook(handler, event=None, resp=response)
                    if inspect.isawaitable(result):
                        await result
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"AstrBot LLM 响应钩子失败 {unit.slug}: {exc}")
                finally:
                    await unit.release_lease()
        if "message" in params:
            params["message"] = response.content
        if "tool_calls" in params:
            params["tool_calls"] = list(response.tool_calls)
        if "reasoning_content" in params:
            params["reasoning_content"] = response.reasoning_content
        if "reasoning_parts" in params and isinstance(response.reasoning_parts, list):
            params["reasoning_parts"] = response.reasoning_parts

    async def _send_segments(
        self, source: Message, segments: list[dict[str, Any]]
    ) -> bool:
        """仅在 QQ profile 使用 MessageService 发送完整 OneBot 段。"""
        validated = _validate_onebot_segments(segments)
        if str(source.platform or "").lower() != "qq":
            raise OneBotProfileError("AstrBot 完整消息链发送仅支持 platform='qq'")
        if str(source.chat_type or "").lower() == "group":
            group_id = source.extra.get("group_id") or source.extra.get(
                "target_group_id"
            )
            response = await self.onebot_send_group_msg(
                source, group_id, validated, False
            )
        else:
            target = source.sender_id or source.extra.get("target_user_id")
            response = await self.onebot_send_private_msg(
                source, target, validated, False
            )
        return bool(response.get("status") == "ok")

    def _message_service(self, message: Message) -> Any:
        """取得并验证 QQ profile 的 MessageService。"""
        self._require_onebot_profile(message)
        service = service_api.get_service(_MESSAGE_SERVICE_SIGNATURE)
        if service is None:
            raise OneBotOperationError(
                f"缺少 OneBot Service: {_MESSAGE_SERVICE_SIGNATURE}"
            )
        return service

    def _account_service(self, message: Message) -> Any:
        """取得并验证 QQ profile 的 AccountService。"""
        self._require_onebot_profile(message)
        service = service_api.get_service(_ACCOUNT_SERVICE_SIGNATURE)
        if service is None:
            raise OneBotOperationError(
                f"缺少 OneBot Service: {_ACCOUNT_SERVICE_SIGNATURE}"
            )
        return service

    @staticmethod
    def _require_onebot_profile(message: Message) -> None:
        """确保调用来自 QQ/OneBot profile。"""
        if str(message.platform or "").lower() != "qq":
            raise OneBotProfileError("OneBot API 仅可用于 platform='qq' 的消息")

    @staticmethod
    def _validate_onebot_response(response: Any) -> dict[str, Any]:
        """只接受明确成功的 OneBot 原始响应，不构造假成功值。"""
        if not isinstance(response, dict):
            raise OneBotOperationError(
                f"OneBot Service 返回非字典响应: {type(response).__name__}"
            )
        if response.get("status") != "ok" or response.get("retcode", 0) != 0:
            raise OneBotOperationError(f"OneBot API 失败: {response}")
        return response

    def _assert_onebot_services_declared(self) -> None:
        """在预备期确认强依赖 Service 已完成组件注册。"""
        missing = [
            signature
            for signature in (_MESSAGE_SERVICE_SIGNATURE, _ACCOUNT_SERVICE_SIGNATURE)
            if service_api.get_service_class(signature) is None
        ]
        if missing:
            raise AstrUnitAdmissionError(
                "缺少强依赖 OneBot Service: " + ", ".join(missing)
            )

    def _rebuild_web_routes(self) -> None:
        """以原子替换方式重建静态 Router 使用的分发表。"""
        candidate: dict[tuple[str, str, str], AstrWebRoute] = {}
        for unit in self.units:
            for route in unit.web_routes:
                for method in route.methods:
                    key = (unit.slug, method, route.route)
                    if key in candidate:
                        raise AstrUnitAdmissionError(f"Web API 路由冲突: {key}")
                    candidate[key] = route
        self._web_routes = candidate

    def _read_provider_task_map(self) -> dict[str, str]:
        """从桥配置读取并验证显式 Provider 到模型任务映射。"""
        config = self.plugin.config
        section = getattr(config, "plugin", None)
        raw_map = getattr(section, "provider_task_map", {})
        if not isinstance(raw_map, dict):
            raise AstrUnitAdmissionError("plugin.provider_task_map 必须为字典")
        result: dict[str, str] = {}
        for provider_id, task_name in raw_map.items():
            normalized_provider = str(provider_id or "").strip()
            normalized_task = str(task_name or "").strip()
            if not normalized_provider or not normalized_task:
                raise AstrUnitAdmissionError(
                    "provider_task_map 的 ID 和模型任务不能为空"
                )
            result[normalized_provider] = normalized_task
        return result

    @staticmethod
    def _resolve_plugin_dirs(config: AstrCompatConfig) -> list[str]:
        """解析单插件路径或桥目录下的外部插件目录。"""
        single = str(config.plugin.plugin_path or "").strip()
        if single:
            return [single]
        root_value = str(config.plugin.plugins_root or "").strip()
        root = (
            Path(root_value).expanduser()
            if root_value
            else Path(__file__).resolve().parent / "plugins"
        )
        if not root.is_dir():
            logger.warning(f"AstrBot 插件仓库不存在: {root}")
            return []
        return [
            str(item)
            for item in sorted(root.iterdir())
            if item.is_dir() and (item / "main.py").is_file()
        ]

    def _active_units(self) -> list[AstrPluginUnit]:
        """返回可接收外部调用的单元列表。"""
        return [unit for unit in self.units if unit.state is AstrUnitState.ACTIVE]


def _is_compatible_component_type(name: str, component_type: type[Any]) -> bool:
    """验证捕获到的组件类型是否满足已声明的 Astr 段契约。"""
    expected_types = {
        "Plain": {"plain", "text"},
        "At": {"at"},
        "Image": {"image"},
        "Record": {"record", "voice"},
        "Reply": {"reply"},
        "Video": {"video"},
        "File": {"file"},
    }
    return (
        str(getattr(component_type, "type", "") or "").lower() in expected_types[name]
    )


def _append_component_candidate(
    candidates: list[type[Any]], component_type: type[Any]
) -> None:
    """按类对象身份向一个组件类别的候选集合追加唯一项。"""
    if component_type not in candidates:
        candidates.append(component_type)


def _component_name_for_type(component_type: type[Any]) -> str | None:
    """按严格 ``type`` 标识识别被外部模块以别名导入的已知组件类。"""
    type_to_name = {
        "plain": "Plain",
        "text": "Plain",
        "at": "At",
        "image": "Image",
        "record": "Record",
        "voice": "Record",
        "reply": "Reply",
        "video": "Video",
        "file": "File",
    }
    return type_to_name.get(str(getattr(component_type, "type", "") or "").lower())


def _make_component_identity_type(
    name: str,
    canonical_type: type[Any] | None,
    unit_types: tuple[type[Any], ...],
    module_prefix: str,
) -> type[Any]:
    """为当前与旧 SDK 类身份生成单元局部兼容类型。"""
    if canonical_type is None:
        if len(unit_types) == 1:
            return unit_types[0]
        raise AstrUnitAdmissionError(f"AstrBot 组件 {name} 缺少当前 SDK 基类")
    bases = tuple(dict.fromkeys((canonical_type, *unit_types)))
    if len(bases) == 1:
        return canonical_type
    try:
        component_type = type(
            f"{name}Compat_{module_prefix.rsplit('_', 1)[-1]}",
            bases,
            {"__module__": module_prefix},
        )
    except TypeError as exc:
        raise AstrUnitAdmissionError(
            f"AstrBot 组件 {name} 与当前 SDK 不能安全统一类身份"
        ) from exc
    return component_type


@register_plugin
class AstrCompatPlugin(BasePlugin):
    """AstrBot 兼容桥插件根组件。"""

    plugin_name = "mofox_for_astr_plugins"
    plugin_description = "在 Neo-MoFox 中运行受限的 AstrBot OneBot Star 插件"
    plugin_version = "0.1.3"
    configs = [AstrCompatConfig]

    def __init__(self, config: AstrCompatConfig | None = None) -> None:
        """在构造期预备外部单元，使动态工具可被宿主组件快照发现。"""
        super().__init__(config)
        self.bridge = AstrPluginBridge(self)
        self._components_frozen = False
        self._component_snapshot: list[type] | None = None
        if isinstance(config, AstrCompatConfig) and config.plugin.enabled:
            self.bridge.prepare_units()

    def get_components(self) -> list[type]:
        """返回加载期固定的事件处理器、Router 和全部动态工具。"""
        if self._component_snapshot is None:
            self._components_frozen = True
            self.bridge.mark_registered()
            components: list[type] = [AstrPluginBridge, AstrCompatRouter]
            for unit in self.bridge.units:
                components.extend(unit.dynamic_tools)
            self._component_snapshot = components
        return list(self._component_snapshot)

    async def on_plugin_loaded(self) -> None:
        """在宿主组件注册完成后激活外部单元。"""
        await self.bridge.activate()

    async def on_plugin_unloaded(self) -> None:
        """按状态门执行单元 drain、任务取消和 Router 卸载。"""
        await self.bridge.unload()


async def _await_optional(value: Any) -> None:
    """调用可调用生命周期钩子，并等待其可等待结果。"""
    result = value() if callable(value) else value
    if inspect.isawaitable(result):
        await result


def _make_slug(path: str) -> str:
    """由目录名和稳定短哈希构建命名空间安全 slug。"""
    resolved = str(Path(path).resolve())
    base = re.sub(r"[^a-z0-9_]+", "_", Path(path).name.lower()).strip("_") or "plugin"
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:8]
    return f"{base}_{digest}"


def _namespaced_tool_name(slug: str, external_name: str) -> str:
    """生成不与其他外部单元冲突的宿主工具名称。"""
    normalized = re.sub(r"[^a-zA-Z0-9_]+", "_", str(external_name or "").strip()).strip(
        "_"
    )
    if not normalized:
        raise AstrUnitAdmissionError("外部工具名称不能为空")
    return f"astr__{slug}__{normalized}"


def _make_handler_tool_class(
    unit: AstrPluginUnit,
    namespaced_name: str,
    external_name: str,
    handler: Callable[..., Any],
) -> type[BaseTool]:
    """为 @filter.llm_tool 生成桥拥有的动态 BaseTool 类型。"""
    tool_name = namespaced_name
    original_name = external_name
    tool_handler = handler
    tool_description = (
        inspect.getdoc(tool_handler) or f"AstrBot 工具 {original_name}"
    ).splitlines()[0]
    schema = _build_handler_schema(
        tool_name, tool_description, tool_handler, excluded={"event"}
    )

    class AstrHandlerTool(BaseTool):
        """桥生成的 AstrBot 声明工具。"""

        name = tool_name
        description = tool_description
        display_name = f"[tool][astr]{original_name}"
        chatter_allow: list[str] = []
        chat_type = ChatType.ALL

        async def execute(self, **kwargs: Any) -> tuple[bool, Any]:
            """转发宿主工具调用到所属外部单元。"""
            return await unit.invoke_llm_tool(tool_handler, **kwargs)

        @classmethod
        def to_schema(cls) -> dict[str, Any]:
            """返回保留命名空间名称的 JSON Schema。"""
            return schema

    AstrHandlerTool.__name__ = f"AstrTool_{unit.slug}_{original_name}"
    AstrHandlerTool.__qualname__ = AstrHandlerTool.__name__
    return AstrHandlerTool


def _make_function_tool_class(
    unit: AstrPluginUnit,
    namespaced_name: str,
    tool: FunctionTool,
) -> type[BaseTool]:
    """为 Context.add_llm_tools(FunctionTool) 生成动态 BaseTool 类型。"""
    tool_name = namespaced_name
    function_tool = tool
    original_name = function_tool.name
    tool_description = function_tool.description or f"AstrBot 工具 {original_name}"
    schema = {
        "type": "function",
        "function": {
            "name": tool_name,
            "description": tool_description,
            "parameters": function_tool.parameters
            or {"type": "object", "properties": {}},
        },
    }

    class AstrFunctionTool(BaseTool):
        """桥生成的 FunctionTool 包装器。"""

        name = tool_name
        description = tool_description
        display_name = f"[tool][astr]{original_name}"
        chatter_allow: list[str] = []
        chat_type = ChatType.ALL

        async def execute(self, **kwargs: Any) -> tuple[bool, Any]:
            """用最新来源事件执行外部 FunctionTool。"""
            await unit.acquire_lease()
            try:
                result = await function_tool.run(unit._latest_event(), **kwargs)
                return True, result
            except Exception as exc:  # noqa: BLE001
                return False, f"工具执行失败: {exc}"
            finally:
                await unit.release_lease()

        @classmethod
        def to_schema(cls) -> dict[str, Any]:
            """返回外部 FunctionTool 的参数 schema。"""
            return schema

    AstrFunctionTool.__name__ = f"AstrFunctionTool_{unit.slug}_{original_name}"
    AstrFunctionTool.__qualname__ = AstrFunctionTool.__name__
    return AstrFunctionTool


def _build_handler_schema(
    name: str,
    description: str,
    handler: Callable[..., Any],
    *,
    excluded: set[str],
) -> dict[str, Any]:
    """根据处理器签名生成最小但完整的 OpenAI function schema。"""
    properties: dict[str, Any] = {}
    required: list[str] = []
    for parameter in inspect.signature(handler).parameters.values():
        if parameter.name in {"self", *excluded} or parameter.kind in {
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        }:
            continue
        annotation = get_origin(parameter.annotation) or parameter.annotation
        json_type = {
            int: "integer",
            float: "number",
            bool: "boolean",
            list: "array",
            dict: "object",
        }.get(annotation, "string")
        field: dict[str, Any] = {
            "type": json_type,
            "description": f"{parameter.name} 参数",
        }
        if parameter.default is not inspect.Parameter.empty:
            field["default"] = parameter.default
        else:
            required.append(parameter.name)
        properties[parameter.name] = field
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


def _strip_wake_prefix(text: str) -> str:
    """剥离单个 wake_prefix（默认 ``/``），模拟 AstrBot 唤醒后的 message_str。

    与 ``str.lstrip`` 不同，仅移除首个匹配的前缀，避免 ``"//foo"`` 被
    全部去掉前导斜杠。wake_prefix 为空字符串时不做处理。
    """
    for prefix in WAKE_PREFIXES:
        if prefix and text.startswith(prefix):
            return text[len(prefix):]
    return text


def _infer_wake_command(message: Message) -> None:
    """推断 AstrBot 唤醒状态并写入 ``message.extra["is_at_or_wake_command"]``。

    AstrBot 唤醒语义（见 ``waking_check/stage.py``）：
    1. 以 wake_prefix（默认 ``/``）开头且群聊首个段不是 @他人 -> 唤醒
    2. @机器人 -> 唤醒
    3. 回复机器人的消息 -> 唤醒
    4. 私聊（默认 ``friend_message_needs_wake_prefix=False``） -> 唤醒

    MoFox OneBotAdapter 不设置该标志，故桥自行推断；已显式设置时不覆盖。
    """
    extra = getattr(message, "extra", None)
    if not isinstance(extra, dict):
        return
    if extra.get("is_at_or_wake_command"):
        return
    text = str(getattr(message, "processed_plain_text", "") or getattr(message, "content", "") or "")
    chat_type = str(getattr(message, "chat_type", "") or "").lower()
    self_id = str(extra.get("self_id", "") or "")
    is_wake = False
    if chat_type == "private":
        is_wake = True
    if not is_wake:
        for prefix in WAKE_PREFIXES:
            if prefix and text.startswith(prefix):
                is_wake = True
                break
    if not is_wake:
        # 优先使用核心解析后的 at_users / reply_to，避免依赖原始 raw_data
        # （OneBotAdapter 不透传 raw_message，但核心 converter 已提取 at 段
        # 并放入 message.extra["at_users"]，reply_to 作为命名属性存在）。
        at_users = extra.get("at_users") or []
        for at_user in at_users:
            if not isinstance(at_user, dict):
                continue
            qq = str(at_user.get("user_id", "") or "")
            if qq and qq == self_id:
                is_wake = True
                break
    if not is_wake and getattr(message, "reply_to", None):
        is_wake = True
    if is_wake:
        extra["is_at_or_wake_command"] = True


def _match_command(entry: _Filter, text: str, is_wake: bool) -> bool:
    """匹配普通或嵌套命令组命令，同时识别每级别名。

    严格遵循 AstrBot 语义：仅在被唤醒（``is_at_or_wake_command`` 为真）时
    匹配，并在匹配前剥离**单个** wake_prefix（默认 ``/``），避免裸命令名
    误触发（如 "help me" 不再匹配 ``/help``）。wake_prefix 可为空字符串，
    表示无前缀要求。
    """
    if not is_wake:
        return False
    stripped = _strip_wake_prefix(text)
    words = stripped.strip().split()
    data = entry.value
    name = str(data.get("name", ""))
    aliases = {name, *set(data.get("alias", set()) or set())}
    group_aliases = data.get("group_path_aliases", ())
    if group_aliases:
        if len(words) < len(group_aliases) + 1:
            return False
        return (
            all(
                word in alternatives
                for word, alternatives in zip(words, group_aliases)
            )
            and words[len(group_aliases)] in aliases
        )
    return bool(words) and words[0] in aliases


def _command_rest(entry: _Filter | None, text: str, is_wake: bool = True) -> str:
    """返回已匹配命令之后的参数字符串。

    与 ``_match_command`` 保持同步：仅在唤醒后剥离单个 wake_prefix，再
    按命令名/组路径消费对应词数，返回剩余参数字符串。
    """
    if entry is None:
        return ""
    stripped = _strip_wake_prefix(text) if is_wake else text
    words = stripped.strip().split()
    data = entry.value
    group_aliases = data.get("group_path_aliases", ())
    consumed = len(group_aliases) + 1 if group_aliases else 1
    return " ".join(words[consumed:])


def _convert_token(token: str, annotation: Any) -> Any:
    """按简单参数注解转换命令 token，失败时保留原字符串。"""
    annotation = get_origin(annotation) or annotation
    try:
        if annotation in {int, "int"}:
            return int(token)
        if annotation in {float, "float"}:
            return float(token)
        if annotation in {bool, "bool"}:
            return token.lower() in {"1", "true", "yes", "on"}
    except ValueError:
        return token
    return token


def _matches_message_type(expected: Any, message: Message) -> bool:
    """严格比较 Astr 消息类型过滤器。"""
    if expected is EventMessageType.ALL:
        return True
    if expected is EventMessageType.GROUP_MESSAGE:
        return message.chat_type == "group"
    if expected is EventMessageType.PRIVATE_MESSAGE:
        return message.chat_type == "private"
    return False


def _is_onebot_adapter_filter(value: Any) -> bool:
    """判定平台过滤器是否只要求 AIOCQHTTP。"""
    normalized = str(getattr(value, "value", value) or "").strip().lower()
    name = str(getattr(value, "name", "") or "").strip().upper()
    return normalized in {"aiocqhttp", "all", "0"} or name in {"AIOCQHTTP", "ALL"}


def _matches_platform(value: Any, message: Message) -> bool:
    """仅 QQ 消息满足 AIOCQHTTP 平台过滤器。"""
    return (
        _is_onebot_adapter_filter(value) and str(message.platform or "").lower() == "qq"
    )


def _is_supported_permission_filter(value: Any) -> bool:
    """验证权限过滤器显式处于桥可映射集合。"""
    level = value.get("level") if isinstance(value, dict) else value
    normalized = str(getattr(level, "value", level) or "").strip().lower()
    return normalized in {"admin", "member"}


async def _matches_permission(value: Any, message: Message) -> bool:
    """使用宿主权限 API 和 sender_role 判定 Astr 权限过滤器。"""
    level = value.get("level") if isinstance(value, dict) else value
    normalized = str(getattr(level, "value", level) or "").strip().lower()
    role = str(message.sender_role or "").lower()
    if normalized == "member":
        return True
    if normalized != "admin":
        return False
    if role in {"owner", "operator", "admin"}:
        return True
    try:
        person_id = permission_api.generate_person_id(
            message.platform, message.sender_id
        )
        permission = await permission_api.get_user_permission_level(person_id)
        return permission >= PermissionLevel.OPERATOR
    except Exception:  # noqa: BLE001
        return False


def _schema_defaults(schema: dict[str, Any]) -> dict[str, Any]:
    """从 AstrBot _conf_schema.json 提取安全默认值。"""
    result: dict[str, Any] = {}
    for key, spec in schema.items():
        if not isinstance(spec, dict):
            continue
        if "default" in spec:
            result[key] = spec["default"]
        elif spec.get("type") == "object" and isinstance(spec.get("items"), dict):
            result[key] = _schema_defaults(spec["items"])
        elif spec.get("type") == "list":
            result[key] = []
        elif spec.get("type") == "bool":
            result[key] = False
        elif spec.get("type") == "int":
            result[key] = 0
        elif spec.get("type") == "float":
            result[key] = 0.0
        else:
            result[key] = ""
    return result


def _as_positive_int(value: int | str | None, name: str) -> int:
    """验证 OneBot 数值目标 ID。"""
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as exc:
        raise OneBotOperationError(f"{name} 必须为正整数") from exc
    if parsed <= 0:
        raise OneBotOperationError(f"{name} 必须为正整数")
    return parsed


def _as_message_id(value: int | str) -> int | str:
    """尽可能把数字消息 ID 规范为整数。"""
    try:
        return int(str(value))
    except ValueError:
        normalized = str(value).strip()
        if not normalized:
            raise OneBotOperationError("message_id 不能为空")
        return normalized


def _validate_onebot_segments(segments: Any) -> list[dict[str, Any]]:
    """验证 MessageService 所需的嵌套 OneBot 段结构。"""
    if not isinstance(segments, list) or not segments:
        raise OneBotOperationError("OneBot message 必须为非空段列表")
    result: list[dict[str, Any]] = []
    for segment in segments:
        if (
            not isinstance(segment, dict)
            or not isinstance(segment.get("type"), str)
            or not isinstance(segment.get("data"), dict)
        ):
            raise OneBotOperationError("OneBot 段必须包含字符串 type 和字典 data")
        result.append({"type": segment["type"], "data": dict(segment["data"])})
    return result


def _build_llm_payloads(
    system_prompt: str, contexts: Iterable[dict[str, Any]], prompt: str
) -> list[LLMPayload]:
    """把 Astr 文本上下文无损映射为宿主文本 LLM payload。"""
    payloads: list[LLMPayload] = []
    if system_prompt:
        payloads.append(LLMPayload(ROLE.SYSTEM, Text(system_prompt)))
    for context in contexts:
        role_name = str(context.get("role", "user")).lower()
        role = ROLE.ASSISTANT if role_name == "assistant" else ROLE.USER
        content = context.get("content", "")
        if isinstance(content, list):
            if any(
                not isinstance(item, dict) or item.get("type") != "text"
                for item in content
            ):
                raise UnsupportedAstrCapabilityError(
                    "Astr LLM context 只支持文本内容段"
                )
            content = "".join(str(item.get("text", "")) for item in content)
        payloads.append(LLMPayload(role, Text(str(content))))
    if prompt:
        payloads.append(LLMPayload(ROLE.USER, Text(prompt)))
    if not payloads:
        raise UnsupportedAstrCapabilityError("Astr LLM 请求至少需要一条文本 payload")
    return payloads


def _payload_text(payloads: Any) -> str:
    """从真实宿主 payload 列表提取可读文本，仅用于钩子观察。"""
    if not isinstance(payloads, list):
        return ""
    texts: list[str] = []
    for payload in payloads:
        for content in getattr(payload, "content", []):
            text = getattr(content, "text", None)
            if isinstance(text, str):
                texts.append(text)
    return "\n".join(texts)


def _call_hook(handler: Callable[..., Any], **kwargs: Any) -> Any:
    """仅向钩子传递其签名接受的关键字参数。"""
    signature = inspect.signature(handler)
    accepts_kwargs = any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )
    if accepts_kwargs:
        return handler(**kwargs)
    accepted = {
        name: value for name, value in kwargs.items() if name in signature.parameters
    }
    return handler(**accepted)


def _invoke_tool_handler(
    handler: Callable[..., Any], event: AstrMessageEvent, kwargs: dict[str, Any]
) -> Any:
    """若外部工具声明 event 参数则注入最新来源事件。"""
    signature = inspect.signature(handler)
    if "event" in signature.parameters:
        return handler(event=event, **kwargs)
    return handler(**kwargs)


__all__ = ["AstrCompatPlugin", "AstrPluginBridge", "AstrPluginUnit", "AstrUnitState"]

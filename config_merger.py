"""AstrBot 兼容桥配置合并管理器。

负责把被兼容 AstrBot 插件的 ``_conf_schema.json`` 配置动态合并到桥的配置类，
使 webui 能展示和编辑被兼容插件的配置。同时提供文件变更轮询，在配置文件被
webui 修改后自动把新配置分发回被兼容插件实例。

与 MaiBot 桥的差异：
- AstrBot 插件配置是扁平 dict（来自 _conf_schema.json），存在 ``unit.config_dict``
- ``Star.config`` 也是 dict，分发只需更新 dict 即可
- schema 需从插件目录的 ``_conf_schema.json`` 重新读取（unit 不保存 schema）
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Callable

from pydantic import Field, create_model
from src.core.managers.config_manager import get_config_manager
from src.kernel.concurrency import get_task_manager
from src.kernel.config import SectionBase
from src.kernel.logger import get_logger

from .config_converter import convert_astr_schema

logger = get_logger("astr_compat_config_merger")


def build_merged_config_class(
    base_config_cls: type,
    plugin_sections: dict[str, dict[str, type[SectionBase]]],
    merged_name: str,
) -> type:
    """构建合并配置类。

    Args:
        base_config_cls: 桥的原配置类（如 AstrCompatConfig）
        plugin_sections: {plugin_slug: {"astr_config": SectionBase 子类}}
        merged_name: 合并配置类的类名

    Returns:
        动态创建的合并配置类（base_config_cls 的子类）
    """
    field_defs: dict[str, Any] = {}
    for field_name, field_info in base_config_cls.model_fields.items():
        ann = field_info.annotation
        if isinstance(ann, type) and issubclass(ann, SectionBase):
            field_defs[field_name] = (ann, field_info)

    for plugin_slug, sections in plugin_sections.items():
        for sec_field_name, sec_cls in sections.items():
            # 字段名必须与 SectionBase 的 __config_section_name__ 一致，
            # 这样 TOML section header 才能与 pydantic 字段名匹配
            merged_field_name = getattr(
                sec_cls, "__config_section_name__", f"astr_{plugin_slug}_{sec_field_name}"
            )
            while merged_field_name.startswith("_"):
                merged_field_name = merged_field_name[1:]
            field_defs[merged_field_name] = (sec_cls, Field(default_factory=sec_cls))

    merged_cls = create_model(
        merged_name,
        __base__=base_config_cls,
        **field_defs,
    )
    logger.debug(f"构建合并配置类 {merged_name}，含 {len(field_defs)} 个 section 字段")
    return merged_cls


def install_merged_config(
    plugin_name: str,
    merged_config_cls: type,
    config_path: Path,
) -> Any:
    """安装合并配置类到 ConfigManager，替换原缓存。"""
    from src.kernel.config import ConfigBase

    manager = get_config_manager()
    config_path = Path(config_path)
    if not config_path.exists():
        config_path.parent.mkdir(parents=True, exist_ok=True)

    if issubclass(merged_config_cls, ConfigBase):
        instance = merged_config_cls.load(config_path, auto_update=True)
    else:
        instance = merged_config_cls()

    manager._configs[plugin_name] = instance  # type: ignore[attr-defined]
    logger.info(f"已安装合并配置类到 ConfigManager: {plugin_name}")
    return instance


def collect_plugin_sections(
    units: list[Any],
) -> tuple[dict[str, dict[str, type[SectionBase]]], dict[str, dict[str, str]]]:
    """收集所有被兼容 AstrBot 插件的配置 section 和字段名映射。

    Args:
        units: AstrPluginUnit 列表（需有 .plugin_dir）

    Returns:
        (plugin_sections, plugin_slug_field_map)
        - plugin_sections: {plugin_slug: {"astr_config": SectionBase 子类}}
        - plugin_slug_field_map: {plugin_slug: {"astr_config": merged_field_name}}
    """
    plugin_sections: dict[str, dict[str, type[SectionBase]]] = {}
    plugin_slug_field_map: dict[str, dict[str, str]] = {}

    for unit in units:
        plugin_dir = getattr(unit, "plugin_dir", "")
        plugin_slug = Path(plugin_dir).name
        schema = _load_schema(Path(plugin_dir))
        if not schema:
            continue
        try:
            sections = convert_astr_schema(schema, plugin_slug)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"转换插件配置失败 {plugin_slug}: {exc}")
            continue
        if not sections:
            continue
        plugin_sections[plugin_slug] = sections
        # 字段名映射使用 __config_section_name__（与 build_merged_config_class 一致）
        field_map: dict[str, str] = {}
        for sec_field_name, sec_cls in sections.items():
            merged_name = getattr(
                sec_cls, "__config_section_name__", f"astr_{plugin_slug}_{sec_field_name}"
            )
            while merged_name.startswith("_"):
                merged_name = merged_name[1:]
            field_map[sec_field_name] = merged_name
        plugin_slug_field_map[plugin_slug] = field_map

    return plugin_sections, plugin_slug_field_map


def _load_schema(plugin_dir: Path) -> dict[str, Any]:
    """读取插件目录的 _conf_schema.json。"""
    schema_file = plugin_dir / "_conf_schema.json"
    if not schema_file.is_file():
        return {}
    try:
        return json.loads(schema_file.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"读取 _conf_schema.json 失败 {plugin_dir}: {exc}")
        return {}


def _unflatten_section_data(flat_data: dict[str, Any]) -> dict[str, Any]:
    """把扁平化字段名（``parent__child``）重组为嵌套 dict。

    与 ``config_converter._convert_field`` 的扁平化策略对应：object 类型的
    子字段以 ``{parent}__{child}`` 命名，这里还原为 ``{parent: {child: ...}}``。
    多层嵌套用 ``__`` 串联，如 ``a__b__c`` -> ``{a: {b: {c: ...}}}``。
    """
    result: dict[str, Any] = {}
    for key, value in flat_data.items():
        if "__" not in key:
            result[key] = value
            continue
        parts = key.split("__")
        cursor = result
        for part in parts[:-1]:
            existing = cursor.get(part)
            if not isinstance(existing, dict):
                existing = {}
                cursor[part] = existing
            cursor = existing
        cursor[parts[-1]] = value
    return result


def apply_toml_data_to_units(
    toml_data: dict[str, Any],
    units: list[Any],
    plugin_slug_field_map: dict[str, dict[str, str]],
) -> None:
    """把 webui 保存的 TOML 数据分发回各被兼容 AstrBot 插件实例。

    AstrBot 配置是扁平 dict，但 webui schema 为避免多页显示已把 object 类型
    扁平化为 ``parent__child`` 字段名。这里先重组为嵌套 dict，再更新
    ``unit.config_dict`` 和 ``instance.config``。

    Args:
        toml_data: 从 config.toml 读取的完整数据
        units: AstrPluginUnit 列表
        plugin_slug_field_map: {plugin_slug: {"astr_config": merged_field_name}}
    """
    for unit in units:
        plugin_dir = getattr(unit, "plugin_dir", "")
        plugin_slug = Path(plugin_dir).name
        slug_map = plugin_slug_field_map.get(plugin_slug)
        if not slug_map:
            continue

        for sec_field_name, merged_field_name in slug_map.items():
            section_data = toml_data.get(merged_field_name)
            if not isinstance(section_data, dict):
                continue
            # 重组扁平化字段为嵌套 dict（autobox__enter -> autobox.enter）
            nested_data = _unflatten_section_data(section_data)
            # 更新 unit.config_dict
            config_dict = getattr(unit, "config_dict", None)
            if isinstance(config_dict, dict):
                config_dict.update(nested_data)
            # 更新 instance.config（Star 实例）
            instance = getattr(unit, "instance", None)
            if instance is not None:
                inst_config = getattr(instance, "config", None)
                if isinstance(inst_config, dict):
                    inst_config.update(nested_data)

        logger.debug(f"AstrBot 插件配置已更新: {plugin_slug}")


class ConfigChangePoller:
    """配置文件变更轮询器。"""

    def __init__(
        self,
        config_path: Path,
        on_change: Callable[[dict[str, Any]], Any],
        poll_interval: float = 5.0,
    ) -> None:
        """初始化轮询器。"""
        self.config_path = Path(config_path)
        self.on_change = on_change
        self.poll_interval = poll_interval
        self._last_mtime: float = 0.0
        self._task_id: str | None = None
        self._running = False

    def start(self) -> None:
        """启动轮询任务。"""
        if self._task_id is not None:
            return
        self._running = True
        try:
            self._last_mtime = self.config_path.stat().st_mtime
        except OSError:
            self._last_mtime = 0.0
        poll_coro = self._poll_loop()
        try:
            task_info = get_task_manager().create_task(
                poll_coro,
                name="astr_config_change_poller",
            )
            self._task_id = task_info.task_id
        except RuntimeError:
            poll_coro.close()
            logger.debug("无事件循环，配置轮询未启动")

    async def stop(self) -> None:
        """停止轮询任务。"""
        self._running = False
        if self._task_id is not None:
            get_task_manager().cancel_task(self._task_id)
            self._task_id = None

    async def _poll_loop(self) -> None:
        """轮询主循环。"""
        while self._running:
            try:
                await asyncio.sleep(self.poll_interval)
                self._check_once()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"配置轮询异常: {exc}")

    def _check_once(self) -> bool:
        """检查一次配置文件是否变更。"""
        try:
            current_mtime = self.config_path.stat().st_mtime
        except OSError:
            return False

        if current_mtime <= self._last_mtime:
            return False

        self._last_mtime = current_mtime
        logger.info(f"检测到配置文件变更: {self.config_path}")

        try:
            import tomllib
            with self.config_path.open("rb") as f:
                data = tomllib.load(f)
            result = self.on_change(data)
            if asyncio.iscoroutine(result):
                try:
                    get_task_manager().create_task(result, name="astr_config_change_callback")
                except RuntimeError:
                    result.close()
                    logger.debug("无事件循环，跳过异步配置变更回调")
        except Exception as exc:  # noqa: BLE001
            logger.error(f"分发配置变更失败: {exc}")
            return False
        return True

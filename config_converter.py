"""AstrBot 插件配置到 MoFox 配置的转换器。

把被兼容 AstrBot 插件的 ``_conf_schema.json`` 转换成 MoFox 的
``SectionBase`` 子类，用于动态生成桥的合并配置类，使 webui 能展示
并编辑被兼容插件的配置。

AstrBot schema 结构（扁平，顶层 key 即字段名）：
- ``type``: int/float/string/text/bool/list/object
- ``default``: 默认值
- ``description``: 描述
- ``hint``: 帮助提示
- ``items``: object 类型的嵌套字段（dict[field_spec]）
- ``item``: list 类型的单项 spec（带 type/description）
- ``slider``: {min, max, step} 数值约束
- ``options``: 枚举选项列表

转换策略：
- 整个 schema 作为一个 SectionBase 子类（section 名 = astr_{plugin_slug}）
- object 类型 -> 嵌套 SectionBase 子类
- list 类型 -> list[基础类型] 字段
- 基础类型 -> 对应 Python 类型字段
"""

from __future__ import annotations

from typing import Any

from src.kernel.config import Field, SectionBase, config_section

# AstrBot schema type -> Python 类型
_ASTR_TYPE_MAP: dict[str, type] = {
    "int": int,
    "float": float,
    "string": str,
    "text": str,
    "bool": bool,
}


def _astr_type_to_python(astr_type: str, spec: dict[str, Any]) -> Any:
    """把 AstrBot schema type 映射到 Python 类型注解。"""
    if astr_type == "list":
        item_spec = spec.get("item", {})
        item_type = item_spec.get("type", "string") if isinstance(item_spec, dict) else "string"
        py_item = _ASTR_TYPE_MAP.get(item_type, str)
        return list[py_item]
    if astr_type == "object":
        return dict  # object 类型用 dict 兜底（嵌套字段单独处理）
    return _ASTR_TYPE_MAP.get(astr_type, str)


def _extract_slider_constraints(spec: dict[str, Any]) -> dict[str, Any]:
    """从 slider 提取 ge/le/step 约束。"""
    result: dict[str, Any] = {}
    slider = spec.get("slider")
    if isinstance(slider, dict):
        if "min" in slider:
            result["ge"] = slider["min"]
        if "max" in slider:
            result["le"] = slider["max"]
        if "step" in slider:
            result["step"] = slider["step"]
    return result


def _convert_object_to_section(
    items: dict[str, Any],
    *,
    section_name: str,
    title: str | None = None,
    doc: str | None = None,
) -> type[SectionBase]:
    """把 AstrBot object 类型的 items 递归转换为 SectionBase 子类。"""
    namespace: dict[str, Any] = {
        "__doc__": doc or "",
        "__qualname__": f"AstrSection_{section_name}",
    }
    new_annotations: dict[str, Any] = {}

    for field_name, field_spec in items.items():
        if not isinstance(field_spec, dict):
            continue
        converted = _convert_field(field_name, field_spec, namespace, new_annotations)
        if not converted:
            # 跳过无法识别的字段
            new_annotations.pop(field_name, None)
            namespace.pop(field_name, None)

    namespace["__annotations__"] = new_annotations
    base = type(
        f"AstrSection_{section_name}",
        (SectionBase,),
        namespace,
    )
    resolved_title = title or section_name
    decorated = config_section(section_name, title=resolved_title)(base)
    return decorated


def _convert_field(
    field_name: str,
    spec: dict[str, Any],
    namespace: dict[str, Any],
    new_annotations: dict[str, Any],
    *,
    prefix: str = "",
) -> bool:
    """转换单个字段到 namespace/new_annotations，返回是否成功。

    object 类型会被扁平化：子字段以 ``{parent}__{child}`` 命名合并到同一
    SectionBase，避免 webui 按嵌套 section 分页显示。分发回插件时由
    ``apply_toml_data_to_units`` 重组为嵌套 dict。

    Args:
        field_name: 原始字段名
        spec: 字段 schema
        namespace: 目标类 namespace
        new_annotations: 目标类注解字典
        prefix: 扁平化字段名前缀（object 递归时使用）
    """
    astr_type = spec.get("type", "string")
    description = spec.get("description", "")
    hint = spec.get("hint")
    default = spec.get("default")

    # object 类型：扁平化子字段到同一 namespace（避免 webui 多页）
    if astr_type == "object" and isinstance(spec.get("items"), dict):
        child_prefix = f"{prefix}{field_name}__" if prefix else f"{field_name}__"
        for child_name, child_spec in spec["items"].items():
            if not isinstance(child_spec, dict):
                continue
            _convert_field(
                child_name, child_spec, namespace, new_annotations, prefix=child_prefix
            )
        return True

    # 扁平化后的最终字段名
    flat_name = f"{prefix}{field_name}" if prefix else field_name
    # 扁平化字段用 description 作 label，避免 webui 显示 parent__child 技术名
    label = description if prefix else None

    # list 类型
    if astr_type == "list":
        py_type = _astr_type_to_python(astr_type, spec)
        if isinstance(default, list):
            factory = _make_mutable_default_factory(default)
            namespace[flat_name] = Field(
                default_factory=factory,
                description=description,
                hint=hint,
                label=label,
                input_type="list",
            )
        else:
            namespace[flat_name] = Field(
                default=[],
                description=description,
                hint=hint,
                label=label,
                input_type="list",
            )
        new_annotations[flat_name] = py_type
        return True

    # 基础类型
    py_type = _ASTR_TYPE_MAP.get(astr_type, str)
    constraints = _extract_slider_constraints(spec)

    # 枚举选项
    options = spec.get("options")
    if isinstance(options, list) and options:
        # 用 Literal 类型实现枚举
        from typing import Literal
        literal_args = tuple(options)
        try:
            py_type = Literal[literal_args]  # type: ignore[valid-type]
        except TypeError:
            pass
        namespace[flat_name] = Field(
            default=default if default is not None else options[0],
            description=description,
            hint=hint,
            label=label,
            choices=options,
        )
        new_annotations[flat_name] = py_type
        return True

    # 带 slider 的数值类型
    if constraints and astr_type in ("int", "float"):
        namespace[flat_name] = Field(
            default=default if default is not None else (0 if astr_type == "int" else 0.0),
            description=description,
            hint=hint,
            label=label,
            input_type="slider",
            **constraints,
        )
        new_annotations[flat_name] = py_type
        return True

    # 普通字段
    if default is None:
        default = "" if astr_type in ("string", "text") else (False if astr_type == "bool" else 0)

    if isinstance(default, (list, dict)):
        factory = _make_mutable_default_factory(default)
        namespace[flat_name] = Field(default_factory=factory, description=description, hint=hint, label=label)
    else:
        namespace[flat_name] = Field(default=default, description=description, hint=hint, label=label)
    new_annotations[flat_name] = py_type
    return True


def _make_mutable_default_factory(value: Any) -> Any:
    """为可变默认值（list/dict）生成 default_factory。"""
    def _factory() -> Any:
        if isinstance(value, list):
            return list(value)
        if isinstance(value, dict):
            return dict(value)
        return value
    return _factory


def convert_astr_schema(
    schema: dict[str, Any],
    plugin_slug: str,
) -> dict[str, type[SectionBase]]:
    """把 AstrBot _conf_schema.json 转换为 SectionBase 子类。

    AstrBot schema 是扁平结构，整体作为一个 section。

    Args:
        schema: _conf_schema.json 解析后的字典
        plugin_slug: 插件目录名，用于生成唯一 section 名

    Returns:
        dict: {"astr_config": SectionBase 子类}（固定单 key）
    """
    if not schema:
        return {}

    section_name = f"astr_{plugin_slug}"
    section_cls = _convert_object_to_section(
        schema,
        section_name=section_name,
        title=f"AstrBot: {plugin_slug}",
    )
    return {"astr_config": section_cls}


def extract_astr_config_data(
    schema: dict[str, Any],
    plugin_slug: str,
) -> dict[str, Any]:
    """提取 AstrBot schema 的默认数据字典。"""
    if not schema:
        return {}

    return {"astr_config": _extract_section_data(schema)}


def _extract_section_data(
    schema: dict[str, Any],
    *,
    prefix: str = "",
) -> dict[str, Any]:
    """递归提取 schema 的默认值，object 类型扁平化（与 _convert_field 一致）。"""
    result: dict[str, Any] = {}
    for key, spec in schema.items():
        if not isinstance(spec, dict):
            continue
        astr_type = spec.get("type", "string")
        flat_name = f"{prefix}{key}" if prefix else key
        if astr_type == "object" and isinstance(spec.get("items"), dict):
            child_prefix = f"{flat_name}__"
            result.update(_extract_section_data(spec["items"], prefix=child_prefix))
        elif "default" in spec:
            result[flat_name] = spec["default"]
        elif astr_type == "int":
            result[flat_name] = 0
        elif astr_type == "float":
            result[flat_name] = 0.0
        elif astr_type == "bool":
            result[flat_name] = False
        elif astr_type == "list":
            result[flat_name] = []
        elif astr_type in ("string", "text"):
            result[flat_name] = ""
        else:
            result[flat_name] = ""
    return result

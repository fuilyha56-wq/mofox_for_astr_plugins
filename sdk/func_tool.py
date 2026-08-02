"""AstrBot 兼容 SDK 的 FunctionTool 与工具集。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class FunctionTool:
    """AstrBot 函数工具基类。"""

    name: str = ""
    description: str = ""
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})
    func_obj: Callable[..., Any] | None = None

    async def run(self, event: Any, **kwargs: Any) -> Any:
        """执行工具。"""
        if self.func_obj is None:
            raise NotImplementedError(f"工具 {self.name} 未实现 run 方法")
        result = self.func_obj(event, **kwargs)
        if hasattr(result, "__await__"):
            return await result
        return result


class ToolSet:
    """工具集合。"""

    def __init__(self) -> None:
        self._tools: dict[str, FunctionTool] = {}

    def add_tool(self, tool: FunctionTool) -> None:
        """添加工具。"""
        self._tools[tool.name] = tool

    def get_tool(self, name: str) -> FunctionTool | None:
        """获取工具。"""
        return self._tools.get(name)

    def get_all_tools(self) -> list[FunctionTool]:
        """返回全部工具。"""
        return list(self._tools.values())


__all__ = ["FunctionTool", "ToolSet"]

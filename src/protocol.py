"""Agent 与决策引擎之间的控制流协议类型。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCall:
    """决策引擎选择调用一个业务工具。

    decision_basis 记录"为什么这么做"（用于 Trace，MockLLM 会填充）。
    """

    tool: str
    args: dict
    decision_basis: str = ""


@dataclass
class Final:
    """决策引擎选择结束，返回最终排期结果。"""

    result: dict
    decision_basis: str = ""


@dataclass
class Observation:
    """一次工具调用的结果观察（进入 Agent 的上下文与 Trace）。"""

    step: int
    tool: str
    args: dict
    result: dict
    decision_basis: str = ""

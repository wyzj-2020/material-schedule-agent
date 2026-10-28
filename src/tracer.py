"""Trace 记录器：记录每一步的工具、参数、返回摘要、后续动作与决策依据。

输出 JSON 满足考核对"运行记录"的要求，便于评审复核两条决策路径。
"""
from __future__ import annotations

import json
import os

from .protocol import Observation


class TraceLogger:
    def __init__(self, meta: dict | None = None):
        self.meta = meta or {}
        self.steps: list[dict] = []
        self.final: dict | None = None

    def record(self, obs: Observation) -> None:
        """记录一步；同时把本步动作回填为上一步的 next_action。"""
        if self.steps:
            self.steps[-1]["next_action"] = obs.tool
        self.steps.append(
            {
                "step": obs.step,
                "tool": obs.tool,
                "args": obs.args,
                "result_summary": obs.result,
                "next_action": None,
                "decision_basis": obs.decision_basis,
            }
        )

    def finish(self, result: dict, unfinished: bool, decision_basis: str = "") -> None:
        if self.steps:
            self.steps[-1]["next_action"] = "finish"
        self.final = {"unfinished": unfinished, "result": result}
        if decision_basis:
            self.final["decision_basis"] = decision_basis

    def to_dict(self) -> dict:
        return {"meta": self.meta, "steps": self.steps, "final": self.final}

    def write(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

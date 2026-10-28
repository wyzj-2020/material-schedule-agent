"""决策引擎：可插拔的模型后端。

两条实现走同一 `decide(context) -> ToolCall | Final` 接口：

- MockLLM：离线确定性策略。它真实读取每次工具返回结果，再据此选择下一步
  （补查 / 替代时段 / 请求确认 / 阻断），并给出决策依据。用于无网环境验证流程，
  不证明真实模型决策效果。
- OpenAICompatLLM：真实模型工具调用（DeepSeek 等 OpenAI 兼容接口）。
"""
from __future__ import annotations

import json
from typing import Any, Optional

from .protocol import Final, Observation, ToolCall
from .schemas import (
    DRAFT_DUPLICATE,
    DRAFT_FAILED,
    DRAFT_NONE,
    DRAFT_SAVED,
    REVIEW_APPROVED,
    REVIEW_BLOCKED,
    REVIEW_INFO_MISSING,
    REVIEW_PENDING,
)

FINISH = "finish"

SYSTEM_PROMPT = """你是内容团队的素材审核与发布排期助手。你的任务是对一批素材，依次完成
"读素材 → 查规则 → 查时段 → 解决冲突 → 存草稿"，最终通过 finish 工具输出结构化排期。

规则：
1. 只能使用提供的工具；save_draft 只保存草稿，绝无"发布"动作。
2. 硬性要求（requirements 中 kind=hard）未经用户同意不得擅自改变：若其目标时段被占用，
   只能请求确认或阻断，不得自动改时段。
3. 软性要求（kind=soft）冲突时，可以给替代时段并放入 pending_confirmations 标记待确认。
4. 规则命中 confirm 动作时，即便时段空闲也须将 review_status 置为 pending_confirmation，
   并把规则原因放入 pending_confirmations；命中 warn 动作时仅记入 risk_reasons。
5. 素材信息缺失或规则命中阻断时，可补查或阻断，不得凭空补齐信息。
6. 只有给出具体时段的素材（review_status 为 approved 或 pending_confirmation）才调用
   save_draft；blocked / info_missing 的素材不保存草稿（draft_status 填 none）。
7. 相同请求重复保存只产生一份草稿（save_draft 会自动去重）。
8. 完成后必须调用 finish(result)，result 结构见 finish 工具描述。
9. 每次工具调用（包括 finish）必须提供 decision_basis：一两句基于已知请求或工具返回事实的
   决策摘要，例如“18:00 已被占用，查询附近空闲时段”。仅记录可核对事实与所选动作，
   不要提供内部思维过程，不要使用“依据观察结果选择该动作”等空泛套话。
10. finish.result.results 中每项必须包含风险原因和待确认项；阻断/信息缺失必须说明具体原因。
    硬性冲突还须明确询问是否同意变更，不能仅在 finish.decision_basis 中说明而遗漏结果字段。
"""


def build_tool_schemas() -> list[dict]:
    """构造真实模型用的 JSON 工具定义。"""
    def fn(name, desc, props, required):
        props = {**props, "decision_basis": {"type": "string", "description": "可核对的事实及动作摘要"}}
        required = [*required, "decision_basis"]
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": desc,
                "parameters": {
                    "type": "object",
                    "properties": props,
                    "required": required,
                },
            },
        }

    return [
        fn("read_material", "读取素材元数据", {"material_id": {"type": "string"}}, ["material_id"]),
        fn("check_rule", "对素材执行审核规则检查", {"material_id": {"type": "string"}}, ["material_id"]),
        fn(
            "query_slots",
            "查询某日期时段占用情况，可限定 start_time/end_time 范围",
            {
                "material_id": {"type": "string"},
                "date": {"type": "string"},
                "start_time": {"type": "string"},
                "end_time": {"type": "string"},
            },
            ["material_id", "date"],
        ),
        fn(
            "save_draft",
            "保存排期草稿（去重、持久化）",
            {
                "material_id": {"type": "string"},
                "date": {"type": "string"},
                "time": {"type": "string"},
                "review_status": {"type": "string"},
                "pending_confirmations": {"type": "array", "items": {"type": "string"}},
                "risk_reasons": {"type": "array", "items": {"type": "string"}},
            },
            ["material_id", "date", "time", "review_status"],
        ),
        fn(
            "finish",
            "结束并返回最终排期结果。result 结构：{results:[{material_id, review_status(approved|blocked|pending_confirmation|info_missing), risk_reasons, suggested_slot:{date,time}|null, pending_confirmations, draft_status(saved|duplicate|failed|none), draft_id, error}], unfinished:false}",
            {"result": {"type": "object", "properties": {
                "results": {"type": "array", "items": {"type": "object", "properties": {
                    "material_id": {"type": "string"},
                    "review_status": {"type": "string", "enum": ["approved", "blocked", "pending_confirmation", "info_missing"]},
                    "risk_reasons": {"type": "array", "items": {"type": "string"}},
                    "suggested_slot": {"type": ["object", "null"], "properties": {
                        "date": {"type": "string"}, "time": {"type": "string"}}, "required": ["date", "time"]},
                    "pending_confirmations": {"type": "array", "items": {"type": "string"}},
                    "draft_status": {"type": "string", "enum": ["saved", "duplicate", "failed", "none"]},
                    "draft_id": {"type": ["string", "null"]},
                    "error": {"type": ["string", "null"]},
                }, "required": ["material_id", "review_status", "risk_reasons", "suggested_slot",
                                  "pending_confirmations", "draft_status", "draft_id", "error"]}},
                "unfinished": {"type": "boolean"},
            }, "required": ["results", "unfinished"]}},
            ["result"],
        ),
    ]


class MockLLM:
    """离线确定性决策策略。

    从 observations 读取工具结果的固定分支策略，验证补查、替代、确认和阻断流程。
    其 decision_basis 是策略生成的说明，不属于真实模型决策证据。
    """

    # 缺失字段名 → 中文标签，用于产出可读的风险原因
    FIELD_LABELS = {
        "duration_minutes": "时长",
        "title": "标题",
        "content_type": "类型",
        "category": "分类",
        "author": "作者",
    }

    def __init__(self, request: dict):
        self.items = request.get("materials", [])
        self.date = request.get("date", "")
        self.idx = 0
        self.results: list[dict] = []
        self._retried: dict[str, bool] = {}

    # ---------- 观察读取 ----------
    @staticmethod
    def _obs_for(obs: list[Observation], mid: str) -> list[Observation]:
        return [o for o in obs if o.args.get("material_id") == mid]

    @staticmethod
    def _last(obs: list[Observation], tool: str) -> Optional[Observation]:
        for o in reversed(obs):
            if o.tool == tool:
                return o
        return None

    @staticmethod
    def _slot_status(slots_result: dict, target: str) -> str:
        for s in slots_result.get("slots", []):
            if s["time"] == target:
                return s["status"]
        return "unknown"

    @staticmethod
    def _slot_occupied_by(slots_result: dict, target: str) -> str:
        for s in slots_result.get("slots", []):
            if s["time"] == target:
                return s.get("occupied_by", "?")
        return "?"

    @staticmethod
    def _nearest_free(slots_result: dict, target: str) -> Optional[str]:
        free = slots_result.get("free_times", [])
        after = [t for t in free if t >= target]
        return after[0] if after else (free[0] if free else None)

    # ---------- 结果构建 ----------
    def _finish_material(
        self,
        mid: str,
        status: str,
        risk_reasons: list[str],
        slot: Optional[dict],
        pending: list[str],
        draft_status: str,
        draft_id: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        self.results.append(
            {
                "material_id": mid,
                "review_status": status,
                "risk_reasons": risk_reasons,
                "suggested_slot": slot,
                "pending_confirmations": pending,
                "draft_status": draft_status,
                "draft_id": draft_id,
                "error": error,
            }
        )

    def _aggregate(self) -> dict:
        rs = self.results
        return {
            "results": rs,
            "summary": {
                "total": len(rs),
                "approved": sum(1 for r in rs if r["review_status"] == REVIEW_APPROVED),
                "blocked": sum(1 for r in rs if r["review_status"] == REVIEW_BLOCKED),
                "pending_confirmation": sum(1 for r in rs if r["review_status"] == REVIEW_PENDING),
                "info_missing": sum(1 for r in rs if r["review_status"] == REVIEW_INFO_MISSING),
                "draft_failed": sum(1 for r in rs if r["draft_status"] == DRAFT_FAILED),
            },
            "unfinished": False,
        }

    def partial_results(self) -> list[dict]:
        """返回当前已完成的素材结果（用于步数上限时携带局部结果）。"""
        return list(self.results)

    # ---------- 决策入口 ----------
    def decide(self, context) -> ToolCall | Final:
        while self.idx < len(self.items):
            item = self.items[self.idx]
            mid = item["material_id"]
            date = item.get("date") or self.date
            target = item["target_time"]
            reqs = item.get("requirements", [])
            is_hard = any(r.get("kind") == "hard" for r in reqs)

            obs = self._obs_for(context.observations, mid)

            # 1) 读取素材
            read = self._last(obs, "read_material")
            if read is None:
                return ToolCall("read_material", {"material_id": mid}, f"开始处理 {mid}，先读取元数据")

            if not read.result.get("found"):
                self._finish_material(mid, REVIEW_BLOCKED, ["素材不存在"], None, [], DRAFT_NONE)
                self.idx += 1
                continue

            missing = read.result.get("missing_fields", [])
            if missing:
                labels = [self.FIELD_LABELS.get(m, m) for m in missing]
                self._finish_material(
                    mid,
                    REVIEW_INFO_MISSING,
                    [f"缺少必填字段: {', '.join(labels)}"],
                    None,
                    ["请补充缺失信息后重新提交"],
                    DRAFT_NONE,
                )
                self.idx += 1
                continue

            # 2) 规则检查
            check = self._last(obs, "check_rule")
            if check is None:
                return ToolCall("check_rule", {"material_id": mid}, "元数据完整，进入规则检查")

            blocking = [r for r in check.result.get("results", []) if r["action"] == "block"]
            if blocking:
                self._finish_material(mid, REVIEW_BLOCKED, [r["message"] for r in blocking], None, [], DRAFT_NONE)
                self.idx += 1
                continue

            confirm_reasons = [r["message"] for r in check.result.get("results", []) if r["action"] == "confirm"]
            warn_reasons = [r["message"] for r in check.result.get("results", []) if r["action"] == "warn"]
            # 非阻断命中都算风险原因；confirm 额外进入待确认项并触发 pending_confirmation，不得直接放行
            risk_reasons = warn_reasons + confirm_reasons

            # 3) 查询目标时段
            exact_q = self._last_exact_query(obs, date, target)
            if exact_q is None:
                return ToolCall(
                    "query_slots",
                    {"material_id": mid, "date": date, "start_time": target, "end_time": target},
                    f"规则无阻断，查询目标时段 {target} 的占用情况",
                )

            if self._slot_status(exact_q.result, target) == "free":
                save = self._last(obs, "save_draft")
                if save is None:
                    if confirm_reasons:
                        # 命中 confirm 规则：即便时段空闲也需用户确认，不得直接放行
                        return self._save_call(
                            mid, date, target, REVIEW_PENDING, risk_reasons, confirm_reasons,
                            f"目标时段 {target} 空闲，但命中需确认规则，标记待确认",
                        )
                    return self._save_call(mid, date, target, REVIEW_APPROVED, risk_reasons, [], f"目标时段 {target} 空闲，直接排期并保存草稿")
                after = self._after_save(mid, save)
                if after is not None:
                    return after
                continue

            # 目标时段被占用
            occupied_by = self._slot_occupied_by(exact_q.result, target)
            if is_hard:
                self._finish_material(
                    mid,
                    REVIEW_BLOCKED,
                    [f"硬性要求时段 {target} 已被 {occupied_by} 占用"],
                    None,
                    [f"硬性要求 {target} 冲突，需用户确认是否变更，未经用户同意不得更改"],
                    DRAFT_NONE,
                )
                self.idx += 1
                continue

            wide_q = self._last_wide_query(obs, date)
            if wide_q is None:
                return ToolCall(
                    "query_slots",
                    {"material_id": mid, "date": date},
                    f"目标时段 {target} 被 {occupied_by} 占用，扩大范围查询可用时段",
                )

            alt = self._nearest_free(wide_q.result, target)
            if alt is None:
                self._finish_material(mid, REVIEW_BLOCKED, ["该日期无可用时段"], None, [], DRAFT_NONE)
                self.idx += 1
                continue

            save = self._last(obs, "save_draft")
            if save is None:
                pending = confirm_reasons + [f"目标时段 {target} 被占用，建议调整为 {alt}，请确认"]
                return self._save_call(mid, date, alt, REVIEW_PENDING, risk_reasons, pending,
                                       f"{target} 被占用，找到最近空闲 {alt}，建议替代并标记待确认")
            after = self._after_save(mid, save)
            if after is not None:
                return after
            continue

        return Final(self._aggregate())

    def _last_exact_query(self, obs: list[Observation], date: str, target: str) -> Optional[Observation]:
        for o in reversed(obs):
            if o.tool == "query_slots" and o.args.get("date") == date and o.args.get("start_time") == target:
                return o
        return None

    def _last_wide_query(self, obs: list[Observation], date: str) -> Optional[Observation]:
        for o in reversed(obs):
            if (
                o.tool == "query_slots"
                and o.args.get("date") == date
                and o.args.get("start_time") is None
                and o.args.get("end_time") is None
            ):
                return o
        return None

    def _save_call(self, mid, date, time, status, risk_reasons, pending, basis) -> ToolCall:
        return ToolCall(
            "save_draft",
            {
                "material_id": mid,
                "date": date,
                "time": time,
                "review_status": status,
                "pending_confirmations": pending,
                "risk_reasons": risk_reasons,
            },
            basis,
        )

    def _after_save(self, mid: str, save: Observation) -> ToolCall | None:
        """处理保存结果：成功/重复则收尾，失败则重试一次后如实上报。"""
        r = save.result
        args = save.args
        slot = {"date": args.get("date"), "time": args.get("time")}
        status = args.get("review_status", REVIEW_APPROVED)
        pending = args.get("pending_confirmations", [])
        risk_reasons = args.get("risk_reasons", [])

        if r.get("saved") and not r.get("duplicate"):
            self._finish_material(mid, status, risk_reasons, slot, pending, DRAFT_SAVED, draft_id=r["draft_id"])
            self.idx += 1
            return None
        if r.get("duplicate"):
            self._finish_material(mid, status, risk_reasons, slot, pending, DRAFT_DUPLICATE, draft_id=r["draft_id"])
            self.idx += 1
            return None
        # 保存失败：重试一次
        if not self._retried.get(mid):
            self._retried[mid] = True
            return ToolCall("save_draft", args, "保存失败，重试一次")
        self._finish_material(mid, status, risk_reasons, slot, pending, DRAFT_FAILED, error=r.get("error"))
        self.idx += 1
        return None


class OpenAICompatLLM:
    """真实模型后端（OpenAI 兼容接口，如 DeepSeek）。需配置 API Key。"""

    def __init__(self, api_key: str, base_url: str, model: str):
        import requests  # 延迟导入，离线 Mock 模式不依赖网络库

        self.requests = requests
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.tools = build_tool_schemas()
        # Provider continuation fields stay in memory only. They are needed to
        # resume tool calling, and are never copied into the public Trace.
        self._turn_messages: dict[int, dict] = {}
        self._call_ids: dict[int, str] = {}
        self._pending_calls: list[dict] = []

    def _take_call(self, context, tc):
        name = tc["function"]["name"]
        args = json.loads(tc["function"].get("arguments") or "{}")
        basis = args.pop("decision_basis", None)
        if not isinstance(basis, str) or not basis.strip():
            raise ValueError("模型工具调用缺少可核对的 decision_basis")
        self._call_ids[context.steps] = tc["id"]
        if name == FINISH:
            return Final(args["result"], basis.strip())
        return ToolCall(name, args, basis.strip())

    def decide(self, context) -> ToolCall | Final:
        if self._pending_calls:
            return self._take_call(context, self._pending_calls.pop(0))
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages.append({"role": "user", "content": json.dumps(context.request, ensure_ascii=False)})
        for o in context.observations:
            if o.step in self._turn_messages:
                messages.append(self._turn_messages[o.step])
            elif o.step not in self._call_ids:
                messages.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": f"call_{o.step}",
                    "type": "function",
                    "function": {"name": o.tool, "arguments": json.dumps(
                        {**o.args, "decision_basis": o.decision_basis}, ensure_ascii=False)},
                }],
                })
            messages.append({
                "role": "tool",
                "tool_call_id": self._call_ids.get(o.step, f"call_{o.step}"),
                "content": json.dumps(o.result, ensure_ascii=False),
            })

        resp = self.requests.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json={"model": self.model, "messages": messages, "tools": self.tools, "tool_choice": "auto"},
            timeout=60,
        )
        resp.raise_for_status()
        msg = resp.json()["choices"][0]["message"]

        if msg.get("tool_calls"):
            calls = msg["tool_calls"]
            if len(calls) > 1 and any(c["function"]["name"] == FINISH for c in calls):
                raise ValueError("finish 不得与其它工具混合调用")
            for i, tc in enumerate(calls):
                tc.setdefault("id", f"call_{context.steps}_{i}")
            assistant = {"role": "assistant", "content": msg.get("content"), "tool_calls": calls}
            if "reasoning_content" in msg:
                assistant["reasoning_content"] = msg["reasoning_content"]
            self._turn_messages[context.steps] = assistant
            self._pending_calls = list(calls[1:])
            return self._take_call(context, calls[0])
        # 模型直接返回文本：尝试解析为最终结果
        content = msg.get("content") or ""
        try:
            return Final(json.loads(content))
        except json.JSONDecodeError:
            return Final({"results": [], "unfinished": True, "reason": "模型未返回有效结果", "raw": content})

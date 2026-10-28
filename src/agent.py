"""ReAct 决策循环：模型选动作 → 执行工具 → 观察结果 → 模型调整下一步或结束。

循环达到步数上限仍无法结束时，返回明确的未完成状态。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .guardrails import apply_guardrails, enforce_boundaries, hard_targets, summarize
from .protocol import Final, Observation, ToolCall
from .schemas import DRAFT_FAILED
from .tools import TOOL_SAVE, execute_tool
from .tracer import TraceLogger
from .validation import validate_request


def _actual_save_outcomes(observations: list[Observation]) -> dict:
    """从 save_draft 观察结果提取每个素材的真实落盘结果（以最后一次调用为准）。

    直接采信工具返回的规范 draft_status（saved / duplicate / failed / none），
    这样"硬性要求被拒（none）"与"保存后端异常（failed）"能正确区分。
    """
    outcomes: dict = {}
    for o in observations:
        if o.tool != TOOL_SAVE:
            continue
        mid = o.args.get("material_id")
        if not isinstance(mid, str):
            continue
        r = o.result
        status = r.get("draft_status")
        stored = r.get("draft") or o.args
        outcomes[mid] = {
            "draft_status": status,
            "draft_id": r.get("draft_id"),
            "error": r.get("error") if status == DRAFT_FAILED else None,
            # 记录实际请求的时段，供守卫对照修正模型虚报的 suggested_slot
            "date": stored.get("date"),
            "time": stored.get("time"),
            "review_status": stored.get("review_status"),
            "risk_reasons": stored.get("risk_reasons", []),
            "pending_confirmations": stored.get("pending_confirmations", []),
            "blocked": r.get("blocked", False),
            "invalid": r.get("invalid", False),
            "rejection_reason": r.get("error"),
        }
    return outcomes


def _partial_from_saved(observations: list[Observation]) -> dict[str, dict]:
    """从 save_draft 观察结果重建已保存素材的局部结果（按素材去重，取最后一次调用）。

    返回 {material_id: result}。覆盖"save_draft 已执行但决策引擎尚未收尾"的边界：
    步数恰好耗尽在保存动作之后时，草稿已落盘（或已失败），仍须进入局部结果。
    """
    out: dict[str, dict] = {}
    for o in observations:
        if o.tool != TOOL_SAVE:
            continue
        r = o.result
        args = o.args
        if not isinstance(args.get("material_id"), str):
            continue
        status = r.get("draft_status") or (
            "duplicate" if r.get("duplicate") else "saved" if r.get("saved") else "failed"
        )
        out[args.get("material_id")] = {
            "material_id": args.get("material_id"),
            "review_status": args.get("review_status"),
            "risk_reasons": args.get("risk_reasons", []),
            "suggested_slot": {"date": args.get("date"), "time": args.get("time")},
            "pending_confirmations": args.get("pending_confirmations", []),
            "draft_status": status,
            "draft_id": r.get("draft_id"),
            "error": r.get("error") if status == DRAFT_FAILED else None,
        }
    return out


def _partial_results(llm, observations: list[Observation]) -> list[dict]:
    """步数上限时的局部结果：合并决策引擎已收尾的素材与工具层已保存的草稿。"""
    by_id: dict[str, dict] = {}
    finished = llm.partial_results() if hasattr(llm, "partial_results") else []
    for r in finished:
        mid = r.get("material_id")
        if mid:
            by_id[mid] = r
    for mid, r in _partial_from_saved(observations).items():
        by_id.setdefault(mid, r)
    return list(by_id.values())


@dataclass
class AgentContext:
    """一次运行的可变状态：请求 + 累计观察 + 步数。"""

    request: dict
    observations: list = field(default_factory=list)
    steps: int = 0
    max_steps: int = 12


def run_agent(request: dict, ctx, llm, max_steps: int = 12, tracer: TraceLogger | None = None) -> dict:
    """执行 ReAct 循环，返回最终排期结果 dict。

    ctx: ToolContext（见 tools.py）；llm: 实现 decide(context) 的决策引擎。
    """
    tracer = tracer or TraceLogger()
    context = AgentContext(request=request, max_steps=max_steps)
    try:
        validate_request(request, max_steps)
    except ValueError as exc:
        result = {"results": [], "summary": {"total": 0}, "unfinished": True,
                  "reason": f"请求无效：{exc}"}
        tracer.finish(result, unfinished=True)
        return result
    ctx.allowed_material_ids = {m["material_id"] for m in request["materials"]}
    ctx.hard_targets = hard_targets(request)

    def incomplete(reason):
        partial = _partial_results(llm, context.observations)
        enforce_boundaries(partial, request, _actual_save_outcomes(context.observations), ctx=ctx)
        result = {"results": partial, "summary": summarize(partial), "unfinished": True,
                  "reason": reason, "steps_executed": context.steps, "completed": len(partial)}
        tracer.finish(result, unfinished=True)
        return result

    while context.steps < max_steps:
        context.steps += 1
        try:
            decision = llm.decide(context)
            if isinstance(decision, Final):
                if not isinstance(decision.result, dict) or not isinstance(decision.result.get("results"), list):
                    raise ValueError("模型结果必须包含 results 列表")
                result = apply_guardrails(decision.result, request, _actual_save_outcomes(context.observations), ctx=ctx)
                tracer.finish(result, unfinished=bool(result.get("unfinished")),
                              decision_basis=decision.decision_basis)
                return result
            if not isinstance(decision, ToolCall):
                raise ValueError("模型未返回有效动作")
        except Exception as exc:
            # Model calls are an external boundary: keep already saved work and
            # explicitly report an unfinished task on timeout/protocol failure.
            status = getattr(getattr(exc, "response", None), "status_code", None)
            label = f"{type(exc).__name__}, HTTP {status}" if status else type(exc).__name__
            return incomplete(f"模型决策失败（{label}），任务未完成")

        try:
            if not isinstance(decision.args, dict):
                raise ValueError("工具参数必须为对象")
            if decision.tool != TOOL_SAVE:
                mid = decision.args.get("material_id")
                if mid not in ctx.allowed_material_ids:
                    raise ValueError("工具素材 ID 缺失或不在本次请求范围内")
            obs_result = execute_tool(ctx, decision.tool, decision.args)
        except Exception as exc:
            # Feed the error back as an observation so the model may correct it.
            obs_result = {"ok": False, "error": f"工具调用失败（{type(exc).__name__}）：{exc}"}
            if decision.tool == TOOL_SAVE:
                obs_result.update(saved=False, draft_status="failed")
            if not isinstance(decision.args, dict):
                decision.args = {}
        obs = Observation(
            step=context.steps,
            tool=decision.tool,
            args=decision.args,
            result=obs_result,
            decision_basis=decision.decision_basis,
        )
        context.observations.append(obs)
        tracer.record(obs)

    # 达到步数上限：明确返回未完成状态，并携带已完成的局部结果
    partial = _partial_results(llm, context.observations)
    # 局部结果同样要过硬性/审核边界与草稿状态对照（不补齐缺失素材）
    enforce_boundaries(partial, request, _actual_save_outcomes(context.observations), ctx=ctx)
    result = {
        "results": partial,
        "summary": summarize(partial),
        "unfinished": True,
        "reason": f"达到执行步数上限（{max_steps} 步），任务未完成",
        "steps_executed": context.steps,
        "completed": len(partial),
    }
    tracer.finish(result, unfinished=True)
    return result

"""守卫层：归一化最终结果 + 兜底硬性要求边界。

真实模型输出的字段用词可能与定义不完全一致（如 review_status 用 passed 而非 approved），
归一化把两种决策引擎（MockLLM / OpenAICompatLLM）的输出统一成同一套 schema。
"""
from __future__ import annotations

from .rules import check_rules
from .validation import validate_date, validate_time
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

_REVIEW_ALIASES = {
    "passed": REVIEW_APPROVED,
    "通过": REVIEW_APPROVED,
    "approved": REVIEW_APPROVED,
    "ok": REVIEW_APPROVED,
    "blocked": REVIEW_BLOCKED,
    "阻断": REVIEW_BLOCKED,
    "pending": REVIEW_PENDING,
    "pending_confirmation": REVIEW_PENDING,
    "待确认": REVIEW_PENDING,
    "info_missing": REVIEW_INFO_MISSING,
    "信息缺失": REVIEW_INFO_MISSING,
}

_DRAFT_ALIASES = {
    "saved": DRAFT_SAVED,
    "duplicate": DRAFT_DUPLICATE,
    "failed": DRAFT_FAILED,
    "none": DRAFT_NONE,
    "": DRAFT_NONE,
}


def _canonical(value: str | None, aliases: dict, default: str) -> str:
    return aliases.get(value, default)


def summarize(results: list[dict]) -> dict:
    return {
        "total": len(results),
        "approved": sum(1 for r in results if r["review_status"] == REVIEW_APPROVED),
        "blocked": sum(1 for r in results if r["review_status"] == REVIEW_BLOCKED),
        "pending_confirmation": sum(1 for r in results if r["review_status"] == REVIEW_PENDING),
        "info_missing": sum(1 for r in results if r["review_status"] == REVIEW_INFO_MISSING),
        "draft_failed": sum(1 for r in results if r["draft_status"] == DRAFT_FAILED),
    }


def normalize_result(result: dict, request: dict) -> dict:
    """把结果统一成规范 schema，并补齐 summary / unfinished 字段。"""
    request_ids = [m["material_id"] for m in request.get("materials", [])]
    normalized: list[dict] = []
    seen = set()
    for r in result.get("results", []):
        if not isinstance(r, dict):
            continue
        mid = r.get("material_id")
        if not isinstance(mid, str) or mid not in request_ids or mid in seen:
            continue
        seen.add(mid)
        draft_status = _canonical(r.get("draft_status"), _DRAFT_ALIASES, DRAFT_NONE)
        # error 只用于保存失败；其它场景统一置空
        error = r.get("error") if draft_status == DRAFT_FAILED else None
        normalized.append(
            {
                "material_id": r.get("material_id"),
                "review_status": _canonical(r.get("review_status"), _REVIEW_ALIASES, REVIEW_BLOCKED),
                "risk_reasons": r.get("risk_reasons") or [],
                "suggested_slot": r.get("suggested_slot"),
                "pending_confirmations": r.get("pending_confirmations") or [],
                "draft_status": draft_status,
                "draft_id": r.get("draft_id"),
                "error": error,
            }
        )

    # 若结果漏掉了某些素材（模型提前结束），补一个信息缺失占位，保证覆盖全部素材
    covered = {r["material_id"] for r in normalized if r.get("material_id")}
    for mid in request_ids:
        if mid not in covered:
            normalized.append(
                {
                    "material_id": mid,
                    "review_status": REVIEW_INFO_MISSING,
                    "risk_reasons": ["未得到最终审核结果"],
                    "suggested_slot": None,
                    "pending_confirmations": [],
                    "draft_status": DRAFT_NONE,
                    "draft_id": None,
                    "error": None,
                }
            )

    result["results"] = normalized
    result.setdefault("unfinished", False)
    result["summary"] = summarize(normalized)
    return result


def _reconcile_drafts(results: list[dict], actual_saves: dict) -> None:
    """对照工具实际落盘结果，修正模型对 draft_status 与 suggested_slot 的虚报。

    - 有实际保存记录的素材，以实际结果为准（saved / duplicate / failed）。
    - 声称 saved/duplicate 但无任何成功保存记录的素材，判定为虚报成功，更正为 none。
    - 成功落盘的素材，报告时段必须与真实草稿时段一致，纠正模型虚报的时段。
    """
    for r in results:
        mid = r.get("material_id")
        actual = actual_saves.get(mid)
        claim = r.get("draft_status")
        if actual is not None:
            r["draft_status"] = actual["draft_status"]
            r["draft_id"] = actual.get("draft_id")
            r["error"] = actual.get("error") if actual["draft_status"] == DRAFT_FAILED else None
            if actual["draft_status"] in (DRAFT_SAVED, DRAFT_DUPLICATE) and actual.get("time"):
                r["suggested_slot"] = {"date": actual.get("date"), "time": actual.get("time")}
                if actual.get("review_status"):
                    r["review_status"] = _canonical(actual["review_status"], _REVIEW_ALIASES, REVIEW_BLOCKED)
                    r["risk_reasons"] = list(actual.get("risk_reasons") or [])
                    r["pending_confirmations"] = list(actual.get("pending_confirmations") or [])
            if actual.get("blocked") or actual.get("invalid"):
                r["review_status"] = REVIEW_INFO_MISSING if actual.get("invalid") else REVIEW_BLOCKED
                r["suggested_slot"] = None
                reason = actual.get("rejection_reason") or "草稿被拒绝保存"
                r.setdefault("risk_reasons", []).append(reason)
        elif claim in (DRAFT_SAVED, DRAFT_DUPLICATE):
            r["draft_status"] = DRAFT_NONE
            r["draft_id"] = None
            r["error"] = None
            r.setdefault("risk_reasons", []).append("模型声称已保存，但工具未记录成功保存，已更正为未保存")


def hard_targets(request: dict) -> dict[str, dict[str, str]]:
    """提取硬性要求锁定的发布时段：material_id -> {"date": 日期, "time": 时刻}。

    硬性要求同时绑定日期与时刻，任一被改都算违约。日期取素材级覆盖，
    否则回落到请求级 date。供保存边界（tools.save_draft）与事后守卫（apply_guardrails）共用，
    避免同一段提取逻辑在两处各写一份。
    """
    req_date = request.get("date", "")
    out: dict[str, dict[str, str]] = {}
    for item in request.get("materials", []):
        reqs = item.get("requirements", [])
        if any(r.get("kind") == "hard" for r in reqs):
            out[item["material_id"]] = {
                "date": item.get("date") or req_date,
                "time": item.get("target_time", ""),
            }
    return out


def _review_verdict(ctx, mid: str):
    """独立重算素材的审核结论：素材缺失信息 / 命中阻断规则。

    返回 (强制 review_status, 原因) 或 (None, None)。与 tools.save_draft 的前置校验
    共用同一套判定，形成「保存前拒绝 + 事后纠正」两道防线，防止模型绕过审核虚报 approved。
    """
    material = (ctx.materials or {}).get(mid)
    if material is None:
        return REVIEW_BLOCKED, "素材不存在"
    if material.missing_fields():
        return REVIEW_INFO_MISSING, "素材信息缺失"
    for r in check_rules(material, ctx.rules or []):
        if not r.passed and r.action == "block":
            return REVIEW_BLOCKED, r.message
    return None, None


def enforce_boundaries(results: list[dict], request: dict, actual_saves: dict | None = None, ctx=None) -> None:
    """对一组已得出的结果施加硬性/审核边界与草稿状态对照（就地修改，不补齐缺失素材）。

    - 硬性要求（日期或时刻任一被改）且状态仍为 approved → 强制 blocked、清空时段。
    - 素材缺失信息 / 命中阻断规则却仍被虚报为 approved/pending → 强制 info_missing/blocked、清空时段。
    - 阻断 / 信息缺失且无具体时段 → draft_status 强制 none（保存失败除外）。
    - 对照工具实际落盘结果，修正 draft_status 与 suggested_slot 虚报。

    正常收尾路径与步数上限路径共用，避免同一套边界逻辑只在一个分支生效。
    """
    # Use the actual saved content before checking constraints, so a false model
    # slot cannot invalidate an otherwise correct stored draft.
    _reconcile_drafts(results, actual_saves or {})
    hard_req = hard_targets(request)
    requested_ids = {m["material_id"] for m in request.get("materials", [])}
    for r in results:
        mid = r.get("material_id")
        if mid not in requested_ids:
            r["review_status"] = REVIEW_BLOCKED
            r["suggested_slot"] = None
            r.setdefault("risk_reasons", []).append("素材不在本次用户请求范围内")
        if r.get("review_status") in (REVIEW_APPROVED, REVIEW_PENDING):
            slot = r.get("suggested_slot")
            try:
                if not isinstance(slot, dict):
                    raise ValueError("缺少建议时段")
                validate_date(slot.get("date"))
                validate_time(slot.get("time"))
            except ValueError as exc:
                r["review_status"] = REVIEW_INFO_MISSING
                r["suggested_slot"] = None
                r.setdefault("risk_reasons", []).append(f"建议时段无效：{exc}")
        required = hard_req.get(mid)
        if required is None:
            continue
        if ctx is not None and required.get("date") and required.get("time"):
            from .tools import _occupied_map
            owner = _occupied_map(ctx, required["date"], mid).get(required["time"])
            if owner and r.get("review_status") != REVIEW_INFO_MISSING:
                r["review_status"] = REVIEW_BLOCKED
                r["suggested_slot"] = None
                reason = f"硬性要求 {required['date']} {required['time']} 已被 {owner} 占用"
                if reason not in r.setdefault("risk_reasons", []):
                    r["risk_reasons"].append(reason)
                if not r.get("pending_confirmations"):
                    r["pending_confirmations"] = ["是否同意变更硬性发布要求？未经用户同意不得更改"]
        actual = (actual_saves or {}).get(mid, {})
        if actual.get("blocked") and actual.get("rejection_reason"):
            reason = actual["rejection_reason"]
            if reason not in r.setdefault("pending_confirmations", []):
                r["pending_confirmations"].append(reason)
        slot = r.get("suggested_slot") or {}
        slot_time = slot.get("time")
        slot_date = slot.get("date")
        violated = (
            (required["time"] and slot_time and slot_time != required["time"])
            or (required["date"] and slot_date and slot_date != required["date"])
        )
        if violated and r.get("review_status") == REVIEW_APPROVED:
            req_label = " ".join(x for x in (required["date"], required["time"]) if x)
            r["review_status"] = REVIEW_BLOCKED
            r["suggested_slot"] = None
            r.setdefault("pending_confirmations", []).append(
                f"硬性要求 {req_label} 未被满足，未经用户同意不得更改"
            )

    # 审核边界：素材缺失信息 / 命中阻断规则时，模型不得虚报为 approved/pending
    if ctx is not None:
        for r in results:
            mid = r.get("material_id")
            if not mid:
                continue
            forced, reason = _review_verdict(ctx, mid)
            if forced is None:
                continue
            if r.get("review_status") not in (REVIEW_BLOCKED, REVIEW_INFO_MISSING):
                r["review_status"] = forced
                r["suggested_slot"] = None
                r.setdefault("risk_reasons", []).append(reason)
            elif not r.get("risk_reasons"):
                r["risk_reasons"] = [reason]

    # 阻断 / 信息缺失的素材不应有"已保存草稿"：无具体时段则强制为 none（与 Mock 语义一致）
    for r in results:
        if r.get("review_status") in (REVIEW_BLOCKED, REVIEW_INFO_MISSING) and r.get("suggested_slot") is None:
            if r.get("draft_status") not in (DRAFT_FAILED,):
                r["draft_status"] = DRAFT_NONE
                r["draft_id"] = None

    for r in results:
        if r.get("review_status") == REVIEW_PENDING and not r.get("pending_confirmations"):
            required = hard_req.get(r.get("material_id"))
            question = (f"是否同意变更硬性要求 {required['date']} {required['time']}？"
                        if required else "请确认建议时段及审核风险后再使用该草稿")
            r["pending_confirmations"] = [question]


def apply_guardrails(result: dict, request: dict, actual_saves: dict | None = None, ctx=None) -> dict:
    """归一化结果，强制硬性/审核边界，并对照工具观察记录修正 draft_status 与时段虚报。"""
    normalize_result(result, request)
    enforce_boundaries(result["results"], request, actual_saves, ctx=ctx)
    result["summary"] = summarize(result["results"])
    return result

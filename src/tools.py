"""业务工具层：素材读取、规则检查、时段查询、排期草稿保存。

工具是 Agent 与"外部世界"交互的唯一通道，返回结构化 dict，供决策引擎观察。
其中 save_draft 是唯一的写操作，只保存草稿、永不发布。
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from threading import RLock
from typing import Any, Optional

from .rules import check_rules
from .file_lock import exclusive_file
from .validation import validate_date, validate_time
from .schemas import DRAFT_DUPLICATE, DRAFT_FAILED, DRAFT_NONE, DRAFT_SAVED, Material, Rule, Slot

# 业务工具名称（外加 finish 作为终止控制，见 llm.py / agent.py）
TOOL_READ = "read_material"
TOOL_CHECK = "check_rule"
TOOL_SLOTS = "query_slots"
TOOL_SAVE = "save_draft"


class SaveBackend:
    """草稿保存后端：负责去重、持久化与失败注入。

    - 去重键由稳定的请求标识 (素材ID, 日期, 时段) 决定，与模型措辞无关，相同请求只产生一份草稿。
    - 结果持久化到 data/drafts.json，使"重复提交"跨进程也可验证。
    - force_fail=True 时模拟保存失败，用于"保存失败"验收场景。
    - fail_persist=True 时模拟落盘抛异常；save 先落盘再更新内存，失败即回滚，不误报成功。
    - 落盘采用"同目录临时文件 + 原子替换"，写入中断不会破坏已有草稿文件。
    """

    def __init__(self, drafts_file: Optional[str] = None, force_fail: bool = False, fail_persist: bool = False):
        self.drafts_file = drafts_file
        self.force_fail = force_fail
        self.fail_persist = fail_persist  # 模拟磁盘写入失败（持久化抛异常），用于测试回滚
        self._drafts: dict[str, dict] = {}  # dedup_key -> draft
        self._order: list[dict] = []
        self._mutex = RLock()
        self._load()

    def _load(self) -> None:
        if not self.drafts_file or not os.path.exists(self.drafts_file):
            return
        with open(self.drafts_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list) or any(not isinstance(d, dict) for d in data):
            raise ValueError("草稿存储格式错误")
        loaded, order = {}, []
        for draft in data:
            # 用当前算法重算去重键，而非信任文件里存的历史值：
            # 去重键算法若升级，历史草稿仍能被正确命中。
            key = self.dedup_key(draft)
            draft["dedup_key"] = key
            loaded[key] = draft
            order.append(draft)
        self._drafts, self._order = loaded, order

    def _persist(self, order: Optional[list[dict]] = None) -> None:
        if not self.drafts_file:
            return
        if self.fail_persist:
            raise OSError("磁盘写入失败（模拟）")
        # 先写同目录临时文件，成功后再原子替换，避免写入中断破坏已有草稿。
        directory = os.path.dirname(os.path.abspath(self.drafts_file))
        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".drafts-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(order if order is not None else self._order, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.drafts_file)
        except Exception:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise

    @staticmethod
    def dedup_key(draft: dict) -> str:
        # 去重键只取稳定的"请求标识"(素材ID, 日期, 时段)，
        # 不纳入 review_status / pending_confirmations 等模型措辞，
        # 否则同一请求换种说法就会被重复落盘成多份草稿。
        raw = (
            draft["material_id"],
            draft.get("date", ""),
            draft.get("time", ""),
        )
        return hashlib.sha1(json.dumps(raw, ensure_ascii=False).encode("utf-8")).hexdigest()

    def save(self, draft: dict) -> dict:
        """保存一份草稿，返回结构化结果（saved / duplicate / failed）。"""
        if self.force_fail:
            return {"saved": False, "draft_status": DRAFT_FAILED, "error": "保存后端异常，草稿未写入"}
        try:
            with self._mutex, exclusive_file(self.drafts_file):
                # Refresh under the same lock as the conflict check and write.
                # A backend loaded earlier must not overwrite newer drafts.
                self._load()
                return self._save_locked(draft)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return {"saved": False, "draft_status": DRAFT_FAILED, "error": f"保存失败：{exc}"}

    def _save_locked(self, draft: dict) -> dict:
        key = self.dedup_key(draft)
        if key in self._drafts:
            existing = self._drafts[key]
            return {
                "saved": True,
                "duplicate": True,
                "draft_status": DRAFT_DUPLICATE,
                "draft_id": existing["draft_id"],
                "draft": dict(existing),
                "note": "相同请求已存在草稿，未重复创建",
            }

        for existing in self._order:
            if (existing.get("date"), existing.get("time")) == (draft.get("date"), draft.get("time")):
                if existing.get("material_id") != draft.get("material_id"):
                    return {"saved": False, "draft_status": DRAFT_FAILED, "conflict": True,
                            "error": "时段冲突：该时段已被其他素材的草稿占用"}

        draft_id = f"D{len(self._order) + 1:03d}"
        record = {"draft_id": draft_id, "dedup_key": key, **draft}
        # 先落盘、成功后再更新内存：持久化失败即回滚，避免"未落盘却误报 duplicate/saved"。
        try:
            self._persist(self._order + [record])
        except OSError as exc:
            return {"saved": False, "draft_status": DRAFT_FAILED, "error": f"保存失败：{exc}"}
        self._drafts[key] = record
        self._order.append(record)
        return {"saved": True, "duplicate": False, "draft_status": DRAFT_SAVED,
                "draft_id": draft_id, "draft": dict(record)}

    def list_drafts(self) -> list[dict]:
        with self._mutex:
            self._load()
            return [dict(d) for d in self._order]


class ToolContext:
    """工具执行所需的共享上下文（素材库、规则库、排期、保存后端）。"""

    def __init__(
        self,
        materials: dict[str, Material],
        rules: list[Rule],
        schedule: dict[str, dict[str, Any]],
        save_backend: SaveBackend,
        hard_targets: Optional[dict[str, dict[str, str]]] = None,
    ):
        self.materials = materials
        self.rules = rules
        self.schedule = schedule
        self.save_backend = save_backend
        # 硬性要求锁定的目标时段：material_id -> target_time。
        # save_draft 在落盘前据此拒绝改变硬性时段的保存请求。
        self.hard_targets = hard_targets or {}
        # Bound by run_agent for each request; None keeps standalone tools usable.
        self.allowed_material_ids: Optional[set[str]] = None
        # 本次运行内已保存草稿的时段预留：date -> {time: material_id}
        # 使 query_slots 能看到"本批次已占用的时段"，避免批量排期冲突。
        self.reservations: dict[str, dict[str, str]] = {}


def read_material(ctx: ToolContext, material_id: str) -> dict:
    """读取素材元数据；不存在或字段缺失时明确返回，供模型决策。"""
    material = ctx.materials.get(material_id)
    if material is None:
        return {"found": False, "material_id": material_id, "error": f"素材 {material_id} 不存在"}
    missing = material.missing_fields()
    return {
        "found": True,
        "material_id": material_id,
        "material": material.to_dict(),
        "missing_fields": missing,
    }


def check_rule(ctx: ToolContext, material_id: str) -> dict:
    """对素材执行规则检查，返回每条规则的命中情况与汇总标记。"""
    material = ctx.materials.get(material_id)
    if material is None:
        return {"found": False, "material_id": material_id, "error": f"素材 {material_id} 不存在"}
    results = check_rules(material, ctx.rules)
    hits = [r for r in results if not r.passed]
    return {
        "found": True,
        "material_id": material_id,
        "results": [r.to_dict() for r in results],
        "any_block": any(r.action == "block" for r in hits),
        "any_risk": any(r.action in ("block", "confirm", "warn") for r in hits),
        "risk_reasons": [r.message for r in hits],
    }


def _occupied_map(ctx: ToolContext, date: str, material_id: Optional[str] = None) -> dict[str, str]:
    """该日期各时段被「其它素材」占用的映射：time -> occupied_by。

    合并三类来源：静态已有排期、已持久化草稿（跨运行）、本次运行预留（批内）。
    查询/保存素材自身的占用不计入，这样重复提交可重新落到同一时段并走去重。
    """
    occ: dict[str, str] = {}
    for t, item in (ctx.schedule.get(date) or {}).items():
        if item and item.get("occupied_by") != material_id:
            occ[t] = item.get("occupied_by", "?")
    for draft in ctx.save_backend.list_drafts():
        if draft.get("date") == date and draft.get("time"):
            by = draft.get("material_id")
            if by != material_id:
                occ[draft["time"]] = by
    for t, mid in (ctx.reservations.get(date) or {}).items():
        if mid != material_id:
            occ[t] = mid
    return occ


def query_slots(ctx: ToolContext, material_id: str, date: str, start_time: Optional[str] = None, end_time: Optional[str] = None) -> dict:
    """查询某日期的时段占用情况，可限定时间范围（用于替代时段补查）。

    时段按 "HH:MM" 排序；start/end 为 None 时返回该日期全部时段。
    除静态排期外，还合并「已保存草稿（跨运行）与本次运行预留（批内）」的占用，避免重复占用时段。
    """
    validate_date(date)
    if start_time is not None:
        validate_time(start_time)
    if end_time is not None:
        validate_time(end_time)
    if start_time and end_time and start_time > end_time:
        raise ValueError("查询起始时刻不能晚于结束时刻")
    day = ctx.schedule.get(date, {})
    occupied = _occupied_map(ctx, date, material_id)
    times = sorted(set(day.keys()) | set(occupied.keys()))
    slots: list[Slot] = []
    for t in times:
        if start_time and t < start_time:
            continue
        if end_time and t > end_time:
            continue
        if t in occupied:
            slots.append(Slot(time=t, status="occupied", occupied_by=occupied[t]))
        else:
            slots.append(Slot(time=t, status="free"))
    return {
        "date": date,
        "material_id": material_id,
        "slots": [s.to_dict() for s in slots],
        "free_times": [s.time for s in slots if s.status == "free"],
    }


def save_draft(ctx: ToolContext, draft: dict) -> dict:
    """保存排期草稿（字段校验、硬性要求前置校验、去重、持久化、失败注入、时段冲突兜底）。"""
    date = draft.get("date")
    time = draft.get("time")
    material_id = draft.get("material_id")

    if ctx.allowed_material_ids is not None and material_id not in ctx.allowed_material_ids:
        return {"saved": False, "draft_status": DRAFT_NONE, "blocked": True,
                "error": "素材不在本次用户请求范围内，未保存"}

    # 字段校验：缺素材 / 日期 / 时段一律拒绝，绝不落盘（无日期的草稿无意义，且会绕过冲突检查）。
    if not material_id or not date or not time:
        return {
            "saved": False,
            "duplicate": False,
            "draft_status": DRAFT_NONE,
            "invalid": True,
            "error": "草稿缺少素材 ID / 日期 / 时段，无法保存",
        }
    try:
        validate_date(date)
        validate_time(time)
    except ValueError as exc:
        return {"saved": False, "draft_status": DRAFT_NONE, "invalid": True,
                "error": f"草稿日期或时刻无效：{exc}"}

    # 审核前置校验：素材不存在 / 信息缺失 / 命中阻断规则时一律拒绝保存，
    # 防止模型绕过审核把"应阻断 / 应补信息"的素材存成 approved/pending。
    material = ctx.materials.get(material_id)
    if material is None:
        return {
            "saved": False,
            "duplicate": False,
            "draft_status": DRAFT_NONE,
            "blocked": True,
            "error": f"素材 {material_id} 不存在，无法保存",
        }
    if material.missing_fields():
        return {
            "saved": False,
            "duplicate": False,
            "draft_status": DRAFT_NONE,
            "invalid": True,
            "error": "素材信息缺失，无法保存草稿",
        }
    blocking = [r for r in check_rules(material, ctx.rules) if not r.passed and r.action == "block"]
    if blocking:
        return {
            "saved": False,
            "duplicate": False,
            "draft_status": DRAFT_NONE,
            "blocked": True,
            "error": f"素材命中阻断规则，无法保存：{blocking[0].message}",
        }

    if draft.get("review_status") not in ("approved", "pending_confirmation"):
        return {"saved": False, "draft_status": DRAFT_NONE, "invalid": True,
                "error": "只有审核通过或待确认的素材可以保存草稿"}
    # Confirmation rules also apply when a real model skips the normal policy.
    confirmations = [r.message for r in check_rules(material, ctx.rules)
                     if not r.passed and r.action == "confirm"]
    if confirmations:
        draft = dict(draft)
        draft["review_status"] = "pending_confirmation"
        draft["pending_confirmations"] = list(dict.fromkeys(
            list(draft.get("pending_confirmations") or []) + confirmations))
        draft["risk_reasons"] = list(dict.fromkeys(
            list(draft.get("risk_reasons") or []) + confirmations))
    if draft.get("review_status") == "pending_confirmation" and not draft.get("pending_confirmations"):
        draft = {**draft, "pending_confirmations": ["请确认建议时段及审核风险后再使用该草稿"]}

    # 硬性要求前置校验：日期或时刻任一被改即拒绝，绝不落盘。
    hard = ctx.hard_targets.get(material_id)
    if hard and (
        (hard.get("time") and time != hard["time"]) or (hard.get("date") and date != hard["date"])
    ):
        return {
            "saved": False,
            "duplicate": False,
            "draft_status": DRAFT_NONE,
            "blocked": True,
            "error": f"硬性要求 {hard.get('date', '')} {hard.get('time', '')} 未被满足，未经用户同意不得更改，未保存",
        }

    other = _occupied_map(ctx, date, material_id).get(time)
    if other:
        return {
            "saved": False,
            "duplicate": False,
            "draft_status": DRAFT_FAILED,
            "conflict": True,
            "error": f"时段冲突：{date} {time} 已被 {other} 占用，未保存",
        }
    result = ctx.save_backend.save(draft)
    if result.get("saved") and not result.get("duplicate"):
        ctx.reservations.setdefault(date, {})[time] = material_id
    return result


def execute_tool(ctx: ToolContext, tool_name: str, args: dict) -> dict:
    """工具分发器：按名称调用对应工具，返回观察结果 dict。"""
    if tool_name == TOOL_READ:
        return read_material(ctx, args["material_id"])
    if tool_name == TOOL_CHECK:
        return check_rule(ctx, args["material_id"])
    if tool_name == TOOL_SLOTS:
        return query_slots(
            ctx,
            args["material_id"],
            args["date"],
            args.get("start_time"),
            args.get("end_time"),
        )
    if tool_name == TOOL_SAVE:
        return save_draft(ctx, args)
    raise ValueError(f"未知工具: {tool_name}")

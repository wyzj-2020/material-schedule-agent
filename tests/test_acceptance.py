"""验收测试：覆盖题面全部 6 个验收点，以及两条动态决策路径、步数上限。"""
import json
import os
import shutil

from src.pipeline import DATA_DIR, run


def _run(request, tmp_path, **kw):
    kw.setdefault("mode", "mock")
    kw.setdefault("data_dir", DATA_DIR)
    kw.setdefault("drafts_file", str(tmp_path / "drafts.json"))
    return run(request, **kw)


def _by_id(result):
    return {r["material_id"]: r for r in result["results"]}


# 1) 正常排期 + 4) 时段冲突（同一输入覆盖两个验收点）
def test_normal_schedule_and_time_conflict(tmp_path):
    request = {
        "date": "2026-10-28",
        "materials": [
            {"material_id": "M01", "target_time": "18:00", "requirements": [{"kind": "soft", "text": "倾向 18:00"}]},
            {"material_id": "M02", "target_time": "10:00", "requirements": [{"kind": "soft", "text": "倾向 10:00"}]},
        ],
    }
    result, tracer = _run(request, tmp_path)
    assert result["unfinished"] is False
    by = _by_id(result)

    # 正常排期：M02 无风险、10:00 空闲 → 通过并保存
    assert by["M02"]["review_status"] == "approved"
    assert by["M02"]["draft_status"] == "saved"
    assert by["M02"]["suggested_slot"]["time"] == "10:00"

    # 时段冲突：M01 目标 18:00 被占 → 建议 19:00 并标记待确认，不得称已发布
    assert by["M01"]["review_status"] == "pending_confirmation"
    assert by["M01"]["suggested_slot"]["time"] == "19:00"
    assert any("19:00" in p for p in by["M01"]["pending_confirmations"])
    assert by["M01"]["draft_status"] == "saved"


# 3) 规则风险
def test_rule_risk(tmp_path):
    request = {
        "date": "2026-10-28",
        "materials": [{"material_id": "M04", "target_time": "10:00", "requirements": [{"kind": "soft", "text": "倾向 10:00"}]}],
    }
    result, _ = _run(request, tmp_path)
    r = result["results"][0]
    assert r["review_status"] == "blocked"
    assert any("敏感" in reason for reason in r["risk_reasons"])
    assert r["draft_status"] == "none"


# 命中 confirm 规则（R3 时长≥60）不得直接放行：空闲时段也应待确认
def test_confirm_rule_requires_confirmation(tmp_path):
    data_dir = str(tmp_path / "data")
    os.makedirs(data_dir)
    for name in ("materials.json", "rules.json", "existing_schedule.json"):
        shutil.copy(os.path.join(DATA_DIR, name), os.path.join(data_dir, name))

    materials_path = os.path.join(data_dir, "materials.json")
    with open(materials_path, encoding="utf-8") as f:
        materials = json.load(f)
    materials["M01"]["duration_minutes"] = 60
    with open(materials_path, "w", encoding="utf-8") as f:
        json.dump(materials, f, ensure_ascii=False, indent=2)

    request = {
        "date": "2026-10-28",
        "materials": [{"material_id": "M01", "target_time": "10:00", "requirements": [{"kind": "soft", "text": "倾向 10:00"}]}],
    }
    result, _ = run(request, mode="mock", data_dir=data_dir, drafts_file=str(tmp_path / "drafts.json"))
    r = result["results"][0]
    assert r["review_status"] == "pending_confirmation"
    assert any("60 分钟" in p for p in r["pending_confirmations"])
    assert r["suggested_slot"]["time"] == "10:00"
    assert r["draft_status"] == "saved"


# 2) 素材信息缺失
def test_missing_info(tmp_path):
    request = {
        "date": "2026-10-28",
        "materials": [{"material_id": "M03", "target_time": "11:00", "requirements": [{"kind": "soft", "text": "倾向 11:00"}]}],
    }
    result, _ = _run(request, tmp_path)
    r = result["results"][0]
    assert r["review_status"] == "info_missing"
    assert any("时长" in reason for reason in r["risk_reasons"])


# 确认边界：硬性要求冲突时不得擅自改时段
def test_hard_requirement_boundary(tmp_path):
    request = {
        "date": "2026-10-28",
        "materials": [{"material_id": "M05", "target_time": "18:00", "requirements": [{"kind": "hard", "text": "必须 18:00"}]}],
    }
    result, _ = _run(request, tmp_path)
    r = result["results"][0]
    assert r["review_status"] == "blocked"
    assert any("未经用户同意" in p for p in r["pending_confirmations"])


# 5) 重复提交：相同请求重复保存只产生一份草稿
def test_duplicate_submission(tmp_path):
    request = {
        "date": "2026-10-28",
        "materials": [{"material_id": "M01", "target_time": "18:00", "requirements": [{"kind": "soft", "text": "倾向 18:00"}]}],
    }
    drafts = str(tmp_path / "drafts.json")
    run(request, mode="mock", data_dir=DATA_DIR, drafts_file=drafts)
    result2, _ = run(request, mode="mock", data_dir=DATA_DIR, drafts_file=drafts)

    assert result2["results"][0]["draft_status"] == "duplicate"
    with open(drafts, encoding="utf-8") as f:
        assert len(json.load(f)) == 1


# 6) 保存失败
def test_save_failure(tmp_path):
    request = {
        "date": "2026-10-28",
        "materials": [{"material_id": "M02", "target_time": "10:00", "requirements": [{"kind": "soft", "text": "倾向 10:00"}]}],
    }
    result, _ = _run(request, tmp_path, fail_save=True)
    r = result["results"][0]
    assert r["draft_status"] == "failed"
    assert r["error"]


# 防虚报：模型在 finish 中声称 saved，但工具从未成功保存，守卫须对照观察记录更正
def test_false_save_report_is_corrected(tmp_path):
    from src.agent import run_agent
    from src.pipeline import build_ctx
    from src.protocol import Final
    from src.tracer import TraceLogger

    request = {
        "date": "2026-10-28",
        "materials": [{"material_id": "M02", "target_time": "10:00", "requirements": [{"kind": "soft", "text": "倾向 10:00"}]}],
    }

    class LyingLLM:
        def decide(self, context):
            return Final({
                "results": [{
                    "material_id": "M02",
                    "review_status": "approved",
                    "suggested_slot": {"date": "2026-10-28", "time": "10:00"},
                    "pending_confirmations": [],
                    "risk_reasons": [],
                    "draft_status": "saved",  # 虚报：从未调用 save_draft
                }]
            })

    ctx = build_ctx(data_dir=DATA_DIR, drafts_file=str(tmp_path / "drafts.json"))
    result = run_agent(request, ctx, LyingLLM(), max_steps=12, tracer=TraceLogger())

    r = result["results"][0]
    assert r["draft_status"] == "none"
    assert r["draft_id"] is None
    assert any("未记录成功保存" in reason for reason in r["risk_reasons"])


# 审核阻断不得被模型绕过：命中 block 规则的素材即便调用 save 并虚报 approved，也要被更正为 blocked
def test_blocked_material_cannot_be_reported_approved(tmp_path):
    from src.agent import run_agent
    from src.pipeline import build_ctx
    from src.protocol import Final, ToolCall
    from src.tracer import TraceLogger

    request = {
        "date": "2026-10-28",
        "materials": [{"material_id": "M04", "target_time": "10:00", "requirements": [{"kind": "soft", "text": "倾向 10:00"}]}],
    }
    drafts_file = str(tmp_path / "drafts.json")

    class BlockBypassLLM:
        def __init__(self):
            self.calls = 0

        def decide(self, context):
            self.calls += 1
            if self.calls == 1:
                return ToolCall(
                    "save_draft",
                    {
                        "material_id": "M04",
                        "date": "2026-10-28",
                        "time": "10:00",
                        "review_status": "approved",
                        "pending_confirmations": [],
                        "risk_reasons": [],
                    },
                    "无视敏感内容阻断规则，直接保存",
                )
            return Final({
                "results": [{
                    "material_id": "M04",
                    "review_status": "approved",
                    "suggested_slot": {"date": "2026-10-28", "time": "10:00"},
                    "pending_confirmations": [],
                    "risk_reasons": [],
                    "draft_status": "saved",
                }]
            })

    ctx = build_ctx(data_dir=DATA_DIR, drafts_file=drafts_file)
    result = run_agent(request, ctx, BlockBypassLLM(), max_steps=12, tracer=TraceLogger())

    r = result["results"][0]
    assert r["review_status"] == "blocked"
    assert r["draft_status"] == "none"
    assert r["suggested_slot"] is None
    assert any("敏感" in reason for reason in r["risk_reasons"])

    # 阻断素材的草稿从未落盘（内存与磁盘皆无）
    assert ctx.save_backend.list_drafts() == []
    assert not os.path.exists(drafts_file)


# 报告时段须与真实草稿一致：保存 10:00 却报告 11:00，守卫须以实际落盘时段为准
def test_reported_slot_must_match_actual_draft(tmp_path):
    from src.agent import run_agent
    from src.pipeline import build_ctx
    from src.protocol import Final, ToolCall
    from src.tracer import TraceLogger

    request = {
        "date": "2026-10-28",
        "materials": [{"material_id": "M02", "target_time": "10:00", "requirements": [{"kind": "soft", "text": "倾向 10:00"}]}],
    }
    drafts_file = str(tmp_path / "drafts.json")

    class SlotLyingLLM:
        def __init__(self):
            self.calls = 0

        def decide(self, context):
            self.calls += 1
            if self.calls == 1:
                return ToolCall(
                    "save_draft",
                    {
                        "material_id": "M02",
                        "date": "2026-10-28",
                        "time": "10:00",
                        "review_status": "approved",
                        "pending_confirmations": [],
                        "risk_reasons": [],
                    },
                    "保存到 10:00",
                )
            return Final({
                "results": [{
                    "material_id": "M02",
                    "review_status": "approved",
                    "suggested_slot": {"date": "2026-10-28", "time": "11:00"},  # 虚报时段
                    "pending_confirmations": [],
                    "risk_reasons": [],
                    "draft_status": "saved",
                }]
            })

    ctx = build_ctx(data_dir=DATA_DIR, drafts_file=drafts_file)
    result = run_agent(request, ctx, SlotLyingLLM(), max_steps=12, tracer=TraceLogger())

    r = result["results"][0]
    assert r["draft_status"] == "saved"
    assert r["suggested_slot"] == {"date": "2026-10-28", "time": "10:00"}

    with open(drafts_file, encoding="utf-8") as f:
        assert json.load(f)[0]["time"] == "10:00"


# 硬性要求前置校验：模型试图把硬性时段改到别处并保存，save_draft 须拒绝且不落盘
def test_hard_requirement_rejected_at_save_boundary(tmp_path):
    from src.agent import run_agent
    from src.guardrails import hard_targets
    from src.pipeline import build_ctx
    from src.protocol import Final, ToolCall
    from src.tracer import TraceLogger

    request = {
        "date": "2026-10-28",
        "materials": [{"material_id": "M05", "target_time": "18:00", "requirements": [{"kind": "hard", "text": "必须 18:00"}]}],
    }
    drafts_file = str(tmp_path / "drafts.json")

    class NaughtyLLM:
        """先调用 save_draft 把硬性素材存到 19:00，再虚报保存成功。"""

        def __init__(self):
            self.calls = 0

        def decide(self, context):
            self.calls += 1
            if self.calls == 1:
                return ToolCall(
                    "save_draft",
                    {
                        "material_id": "M05",
                        "date": "2026-10-28",
                        "time": "19:00",
                        "review_status": "approved",
                        "pending_confirmations": [],
                        "risk_reasons": [],
                    },
                    "无视硬性要求，擅自把 18:00 改为 19:00",
                )
            return Final({
                "results": [{
                    "material_id": "M05",
                    "review_status": "approved",
                    "suggested_slot": {"date": "2026-10-28", "time": "19:00"},
                    "pending_confirmations": [],
                    "risk_reasons": [],
                    "draft_status": "saved",
                }]
            })

    ctx = build_ctx(data_dir=DATA_DIR, drafts_file=drafts_file, hard_targets=hard_targets(request))
    result = run_agent(request, ctx, NaughtyLLM(), max_steps=12, tracer=TraceLogger())

    r = result["results"][0]
    assert r["review_status"] == "blocked"
    assert r["suggested_slot"] is None
    assert r["draft_status"] == "none"
    assert r["draft_id"] is None
    assert any("未经用户同意" in p for p in r["pending_confirmations"])

    # 关键：硬性违反的草稿从未落盘（内存与磁盘皆无）
    assert ctx.save_backend.list_drafts() == []
    assert not os.path.exists(drafts_file)


# 组合边界：硬性要求被拒绝，同时耗尽步数，局部结果须为 blocked 而非模型声称的 approved
def test_hard_rejection_combined_with_step_limit(tmp_path):
    from src.agent import run_agent
    from src.guardrails import hard_targets
    from src.pipeline import build_ctx
    from src.protocol import ToolCall
    from src.tracer import TraceLogger

    request = {
        "date": "2026-10-28",
        "materials": [{"material_id": "M05", "target_time": "18:00", "requirements": [{"kind": "hard", "text": "必须 18:00"}]}],
    }
    drafts_file = str(tmp_path / "drafts.json")

    class HardViolatingLLM:
        def decide(self, context):
            return ToolCall(
                "save_draft",
                {
                    "material_id": "M05",
                    "date": "2026-10-28",
                    "time": "19:00",
                    "review_status": "approved",
                    "pending_confirmations": [],
                    "risk_reasons": [],
                },
                "试图把硬性素材改到 19:00",
            )

    ctx = build_ctx(data_dir=DATA_DIR, drafts_file=drafts_file, hard_targets=hard_targets(request))
    result = run_agent(request, ctx, HardViolatingLLM(), max_steps=1, tracer=TraceLogger())

    assert result["unfinished"] is True
    r = result["results"][0]
    assert r["material_id"] == "M05"
    assert r["review_status"] == "blocked"
    assert r["draft_status"] == "none"
    assert r["suggested_slot"] is None
    assert ctx.save_backend.list_drafts() == []


# 硬性要求绑定日期：模型把硬性素材改到次日（时刻不变）保存，save_draft 须拒绝且不落盘
def test_hard_requirement_date_change_rejected_at_save_boundary(tmp_path):
    from src.agent import run_agent
    from src.guardrails import hard_targets
    from src.pipeline import build_ctx
    from src.protocol import Final, ToolCall
    from src.tracer import TraceLogger

    request = {
        "date": "2026-10-28",
        "materials": [{"material_id": "M05", "target_time": "18:00", "requirements": [{"kind": "hard", "text": "必须 18:00"}]}],
    }
    drafts_file = str(tmp_path / "drafts.json")

    class DateShiftLLM:
        def __init__(self):
            self.calls = 0

        def decide(self, context):
            self.calls += 1
            if self.calls == 1:
                return ToolCall(
                    "save_draft",
                    {
                        "material_id": "M05",
                        "date": "2026-10-29",  # 次日，时刻仍 18:00
                        "time": "18:00",
                        "review_status": "approved",
                        "pending_confirmations": [],
                        "risk_reasons": [],
                    },
                    "把硬性要求改到次日",
                )
            return Final({
                "results": [{
                    "material_id": "M05",
                    "review_status": "approved",
                    "suggested_slot": {"date": "2026-10-29", "time": "18:00"},
                    "pending_confirmations": [],
                    "risk_reasons": [],
                    "draft_status": "saved",
                }]
            })

    ctx = build_ctx(data_dir=DATA_DIR, drafts_file=drafts_file, hard_targets=hard_targets(request))
    result = run_agent(request, ctx, DateShiftLLM(), max_steps=12, tracer=TraceLogger())

    r = result["results"][0]
    assert r["review_status"] == "blocked"
    assert r["suggested_slot"] is None
    assert r["draft_status"] == "none"
    assert any("未经用户同意" in p for p in r["pending_confirmations"])

    assert ctx.save_backend.list_drafts() == []
    assert not os.path.exists(drafts_file)


# 批量排期无冲突：同一日期多个素材不得占用同一时段
def test_batch_scheduling_no_conflict(tmp_path):
    request = {
        "date": "2026-10-28",
        "materials": [
            {"material_id": "M01", "target_time": "18:00", "requirements": [{"kind": "soft", "text": "倾向 18:00"}]},
            {"material_id": "M02", "target_time": "19:00", "requirements": [{"kind": "soft", "text": "倾向 19:00"}]},
        ],
    }
    result, _ = _run(request, tmp_path)
    by = _by_id(result)

    times = [by[m]["suggested_slot"]["time"] for m in ("M01", "M02")]
    assert len(times) == len(set(times)), f"批量排期出现时段冲突: {times}"

    # M01 先占 18:00 落空改 19:00；M02 的 19:00 已被本批预留，须落到其它空闲时段
    assert by["M01"]["suggested_slot"]["time"] == "19:00"
    assert by["M02"]["suggested_slot"]["time"] != "19:00"
    assert all(by[m]["draft_status"] == "saved" for m in ("M01", "M02"))


# 跨运行无冲突：M01 先单独排期，M02 后续单独提交同一时段，须被识别为占用
def test_cross_run_separate_submission_no_conflict(tmp_path):
    drafts = str(tmp_path / "drafts.json")
    req_m01 = {"date": "2026-10-28", "materials": [{"material_id": "M01", "target_time": "10:00", "requirements": [{"kind": "soft", "text": "倾向 10:00"}]}]}
    req_m02 = {"date": "2026-10-28", "materials": [{"material_id": "M02", "target_time": "10:00", "requirements": [{"kind": "soft", "text": "倾向 10:00"}]}]}

    r1, _ = run(req_m01, mode="mock", data_dir=DATA_DIR, drafts_file=drafts)
    assert r1["results"][0]["suggested_slot"]["time"] == "10:00"
    assert r1["results"][0]["draft_status"] == "saved"

    r2, _ = run(req_m02, mode="mock", data_dir=DATA_DIR, drafts_file=drafts)
    m02 = r2["results"][0]
    # M01 的 10:00 草稿已持久化，M02 不应再占 10:00
    assert m02["suggested_slot"]["time"] != "10:00"
    assert m02["review_status"] == "pending_confirmation"
    assert m02["draft_status"] == "saved"

    with open(drafts, encoding="utf-8") as f:
        saved_times = {d["material_id"]: d["time"] for d in json.load(f)}
    assert saved_times["M01"] != saved_times["M02"]


# 先保存 M01，再批量提交 M01、M02：M01 去重，M02 避开 M01 已占时段
def test_batch_after_existing_single_draft(tmp_path):
    drafts = str(tmp_path / "drafts.json")
    req_m01 = {"date": "2026-10-28", "materials": [{"material_id": "M01", "target_time": "10:00", "requirements": [{"kind": "soft", "text": "倾向 10:00"}]}]}
    run(req_m01, mode="mock", data_dir=DATA_DIR, drafts_file=drafts)

    req_batch = {
        "date": "2026-10-28",
        "materials": [
            {"material_id": "M01", "target_time": "10:00", "requirements": [{"kind": "soft", "text": "倾向 10:00"}]},
            {"material_id": "M02", "target_time": "10:00", "requirements": [{"kind": "soft", "text": "倾向 10:00"}]},
        ],
    }
    result, _ = run(req_batch, mode="mock", data_dir=DATA_DIR, drafts_file=drafts)
    by = _by_id(result)

    # M01 已有相同草稿 → duplicate，不新增
    assert by["M01"]["draft_status"] == "duplicate"
    # M02 的 10:00 已被 M01 占用 → 落到其它时段
    assert by["M02"]["suggested_slot"]["time"] != "10:00"
    assert by["M02"]["draft_status"] == "saved"

    with open(drafts, encoding="utf-8") as f:
        assert len(json.load(f)) == 2  # 仅 M01 + M02 两份草稿


# 步数上限：达到上限必须返回明确的未完成状态
def test_step_limit_returns_unfinished(tmp_path):
    request = {
        "date": "2026-10-28",
        "materials": [
            {"material_id": "M01", "target_time": "18:00", "requirements": [{"kind": "soft", "text": "倾向 18:00"}]},
            {"material_id": "M02", "target_time": "10:00", "requirements": [{"kind": "soft", "text": "倾向 10:00"}]},
        ],
    }
    result, _ = _run(request, tmp_path, max_steps=1)
    assert result["unfinished"] is True
    assert "上限" in result["reason"]


# 步数上限时须携带已完成的局部结果（而非 results=[]），与已落盘草稿一致
def test_step_limit_carries_partial_results(tmp_path):
    request = {
        "date": "2026-10-28",
        "materials": [
            {"material_id": "M01", "target_time": "18:00", "requirements": [{"kind": "soft", "text": "倾向 18:00"}]},
            {"material_id": "M02", "target_time": "18:00", "requirements": [{"kind": "soft", "text": "倾向 18:00"}]},
            {"material_id": "M05", "target_time": "18:00", "requirements": [{"kind": "soft", "text": "倾向 18:00"}]},
        ],
    }
    result, _ = _run(request, tmp_path, max_steps=12)
    assert result["unfinished"] is True
    assert "上限" in result["reason"]

    # 前两个素材已完成并保存，须出现在 results 中
    ids = [r["material_id"] for r in result["results"]]
    assert ids == ["M01", "M02"]
    assert all(r["draft_status"] == "saved" for r in result["results"])
    assert result["completed"] == 2

    # 已保存的草稿确实落盘，与局部结果一致
    with open(str(tmp_path / "drafts.json"), encoding="utf-8") as f:
        assert len(json.load(f)) == 2


# 步数恰好耗尽在保存动作之后：草稿已落盘，局部结果不得为空
def test_step_limit_at_save_boundary_carries_saved_draft(tmp_path):
    request = {
        "date": "2026-10-28",
        "materials": [{"material_id": "M02", "target_time": "10:00", "requirements": [{"kind": "soft", "text": "倾向 10:00"}]}],
    }
    result, _ = _run(request, tmp_path, max_steps=4)
    assert result["unfinished"] is True
    assert result["completed"] == 1

    r = result["results"][0]
    assert r["material_id"] == "M02"
    assert r["draft_status"] == "saved"
    assert r["suggested_slot"]["time"] == "10:00"

    with open(str(tmp_path / "drafts.json"), encoding="utf-8") as f:
        assert len(json.load(f)) == 1


# 保存持续失败且恰好耗尽步数：局部结果须携带 failed 与具体原因
def test_step_limit_at_save_boundary_carries_failed_draft(tmp_path):
    request = {
        "date": "2026-10-28",
        "materials": [{"material_id": "M02", "target_time": "10:00", "requirements": [{"kind": "soft", "text": "倾向 10:00"}]}],
    }
    result, _ = _run(request, tmp_path, max_steps=5, fail_save=True)
    assert result["unfinished"] is True
    assert result["completed"] == 1

    r = result["results"][0]
    assert r["material_id"] == "M02"
    assert r["draft_status"] == "failed"
    assert r["error"]


# 两条执行路径：模型依据不同工具结果选择不同动作
def test_dynamic_decision_paths(tmp_path):
    # 路径 A：时段冲突 → 补查附近时段 → 替代并待确认
    req_a = {
        "date": "2026-10-28",
        "materials": [{"material_id": "M01", "target_time": "18:00", "requirements": [{"kind": "soft", "text": "倾向 18:00"}]}],
    }
    _, tracer_a = _run(req_a, tmp_path)
    tools_a = [s["tool"] for s in tracer_a.steps]
    assert tools_a == ["read_material", "check_rule", "query_slots", "query_slots", "save_draft"]

    # 路径 B：规则命中阻断 → 不再查时段，直接结束
    req_b = {
        "date": "2026-10-28",
        "materials": [{"material_id": "M04", "target_time": "10:00", "requirements": [{"kind": "soft", "text": "倾向 10:00"}]}],
    }
    _, tracer_b = _run(req_b, tmp_path)
    tools_b = [s["tool"] for s in tracer_b.steps]
    assert tools_b == ["read_material", "check_rule"]
    assert "query_slots" not in tools_b

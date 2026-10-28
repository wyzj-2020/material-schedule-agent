"""工具层单元测试：素材读取、规则检查、时段查询、草稿保存。"""
import json
import os

from src import tools
from src.pipeline import DATA_DIR, build_ctx


def test_read_material_found(ctx):
    r = tools.read_material(ctx, "M01")
    assert r["found"] is True
    assert r["missing_fields"] == []


def test_read_material_missing_duration(ctx):
    r = tools.read_material(ctx, "M03")
    assert r["found"] is True
    assert "duration_minutes" in r["missing_fields"]


def test_read_material_not_found(ctx):
    r = tools.read_material(ctx, "M999")
    assert r["found"] is False
    assert "不存在" in r["error"]


def test_check_rule_sensitive_blocks(ctx):
    r = tools.check_rule(ctx, "M04")
    assert r["any_block"] is True
    assert any("敏感" in reason for reason in r["risk_reasons"])


def test_check_rule_clean(ctx):
    r = tools.check_rule(ctx, "M02")
    assert r["any_block"] is False
    assert r["risk_reasons"] == []


def test_query_slots_occupancy(ctx):
    r = tools.query_slots(ctx, "M01", "2026-10-28")
    status = {s["time"]: s for s in r["slots"]}
    assert status["18:00"]["status"] == "occupied"
    assert status["18:00"]["occupied_by"] == "M99"
    assert status["19:00"]["status"] == "free"


def test_query_slots_range_filter(ctx):
    r = tools.query_slots(ctx, "M01", "2026-10-28", start_time="18:00", end_time="19:00")
    assert [s["time"] for s in r["slots"]] == ["18:00", "19:00"]


def test_save_draft_dedup(ctx):
    draft = {
        "material_id": "M01",
        "date": "2026-10-28",
        "time": "19:00",
        "review_status": "pending_confirmation",
        "pending_confirmations": ["建议调整"],
        "risk_reasons": [],
    }
    r1 = tools.save_draft(ctx, draft)
    assert r1["saved"] is True and r1["duplicate"] is False
    assert r1["draft_status"] == "saved"

    r2 = tools.save_draft(ctx, draft)
    assert r2["duplicate"] is True
    assert r2["draft_status"] == "duplicate"
    assert r2["draft_id"] == r1["draft_id"]
    assert len(ctx.save_backend.list_drafts()) == 1


def test_save_draft_dedup_ignores_confirmation_wording(ctx):
    """去重键须是稳定请求标识：同一素材/日期/时段，仅待确认文本换说法仍视为重复。"""
    d1 = {
        "material_id": "M01",
        "date": "2026-10-28",
        "time": "19:00",
        "review_status": "pending_confirmation",
        "pending_confirmations": ["目标时段 18:00 被占用，建议调整为 19:00，请确认"],
        "risk_reasons": [],
    }
    r1 = tools.save_draft(ctx, d1)
    assert r1["saved"] is True and r1["duplicate"] is False

    d2 = {
        "material_id": "M01",
        "date": "2026-10-28",
        "time": "19:00",
        "review_status": "pending_confirmation",
        "pending_confirmations": ["18:00 已占用，请确认是否改到 19:00"],  # 措辞不同
        "risk_reasons": [],
    }
    r2 = tools.save_draft(ctx, d2)
    assert r2["duplicate"] is True
    assert r2["draft_status"] == "duplicate"
    assert r2["draft_id"] == r1["draft_id"]
    assert len(ctx.save_backend.list_drafts()) == 1


def test_save_draft_failure(tmp_path):
    ctx = build_ctx(data_dir=DATA_DIR, drafts_file=str(tmp_path / "drafts.json"), fail_save=True)
    draft = {
        "material_id": "M02",
        "date": "2026-10-28",
        "time": "10:00",
        "review_status": "approved",
        "pending_confirmations": [],
        "risk_reasons": [],
    }
    r = tools.save_draft(ctx, draft)
    assert r["saved"] is False
    assert r["draft_status"] == "failed"
    assert r["error"]


def test_save_rollback_on_persist_failure(tmp_path):
    """落盘失败须回滚：不得把未落盘的草稿留在内存里，导致后续误报 duplicate。"""
    backend = tools.SaveBackend(drafts_file=str(tmp_path / "drafts.json"), fail_persist=True)
    draft = {
        "material_id": "M02",
        "date": "2026-10-28",
        "time": "10:00",
        "review_status": "approved",
        "pending_confirmations": [],
        "risk_reasons": [],
    }

    # 首次落盘抛异常：如实上报失败
    r1 = backend.save(draft)
    assert r1["saved"] is False
    assert r1["draft_status"] == "failed"
    assert "失败" in r1["error"]

    # 恢复后再次保存：应成功，而非误报 duplicate；磁盘上只有一份草稿
    backend.fail_persist = False
    r2 = backend.save(draft)
    assert r2["saved"] is True
    assert r2["duplicate"] is False
    assert r2["draft_status"] == "saved"
    assert len(backend.list_drafts()) == 1

    with open(backend.drafts_file, encoding="utf-8") as f:
        assert len(json.load(f)) == 1


def test_persist_failure_preserves_existing_file(tmp_path):
    """落盘写入失败不得破坏已有草稿文件（临时文件 + 原子替换）。"""
    drafts_file = str(tmp_path / "drafts.json")
    seed = tools.SaveBackend(drafts_file=drafts_file)
    seed.save({
        "material_id": "M01",
        "date": "2026-10-28",
        "time": "19:00",
        "review_status": "pending_confirmation",
        "pending_confirmations": ["x"],
        "risk_reasons": [],
    })
    before = open(drafts_file, encoding="utf-8").read()

    # 模拟写入失败，保存另一份草稿
    backend = tools.SaveBackend(drafts_file=drafts_file, fail_persist=True)
    r = backend.save({
        "material_id": "M02",
        "date": "2026-10-28",
        "time": "10:00",
        "review_status": "approved",
        "pending_confirmations": [],
        "risk_reasons": [],
    })
    assert r["saved"] is False

    # 已有文件内容未被破坏，且无残留临时文件
    assert open(drafts_file, encoding="utf-8").read() == before
    leftovers = [n for n in os.listdir(tmp_path) if n.endswith(".tmp") or n.startswith(".drafts-")]
    assert leftovers == []


def test_save_draft_blocks_slot_conflict(ctx):
    """批量排期冲突兜底：同一时段被另一素材预留后，再次保存须失败。"""
    d1 = {
        "material_id": "M01",
        "date": "2026-10-28",
        "time": "19:00",
        "review_status": "pending_confirmation",
        "pending_confirmations": ["建议调整"],
        "risk_reasons": [],
    }
    assert tools.save_draft(ctx, d1)["saved"] is True

    d2 = {
        "material_id": "M02",
        "date": "2026-10-28",
        "time": "19:00",
        "review_status": "approved",
        "pending_confirmations": [],
        "risk_reasons": [],
    }
    r = tools.save_draft(ctx, d2)
    assert r["saved"] is False
    assert r["conflict"] is True
    assert "冲突" in r["error"]


def test_query_slots_merges_run_reservations(ctx):
    """本次运行已保存草稿的时段，须在 query_slots 中表现为 occupied。"""
    tools.save_draft(ctx, {
        "material_id": "M01",
        "date": "2026-10-28",
        "time": "19:00",
        "review_status": "pending_confirmation",
        "pending_confirmations": ["建议调整"],
        "risk_reasons": [],
    })
    r = tools.query_slots(ctx, "M02", "2026-10-28")
    status = {s["time"]: s for s in r["slots"]}
    assert status["19:00"]["status"] == "occupied"
    assert status["19:00"]["occupied_by"] == "M01"
    assert "19:00" not in r["free_times"]


def test_save_draft_rejects_hard_target_violation(tmp_path):
    """硬性要求须在保存前校验：时段不符即拒绝，且不落盘。"""
    ctx = build_ctx(
        data_dir=DATA_DIR,
        drafts_file=str(tmp_path / "drafts.json"),
        hard_targets={"M05": {"date": "2026-10-28", "time": "18:00"}},
    )
    draft = {
        "material_id": "M05",
        "date": "2026-10-28",
        "time": "19:00",
        "review_status": "approved",
        "pending_confirmations": [],
        "risk_reasons": [],
    }
    r = tools.save_draft(ctx, draft)
    assert r["saved"] is False
    assert r["duplicate"] is False
    assert r["draft_status"] == "none"
    assert r["blocked"] is True
    assert "未经用户同意" in r["error"]
    assert ctx.save_backend.list_drafts() == []


def test_save_draft_rejects_hard_date_change(tmp_path):
    """硬性要求同时绑定日期：改到次日、时刻不变，同样拒绝且不落盘。"""
    ctx = build_ctx(
        data_dir=DATA_DIR,
        drafts_file=str(tmp_path / "drafts.json"),
        hard_targets={"M05": {"date": "2026-10-28", "time": "18:00"}},
    )
    draft = {
        "material_id": "M05",
        "date": "2026-10-29",  # 次日，时刻仍为 18:00
        "time": "18:00",
        "review_status": "approved",
        "pending_confirmations": [],
        "risk_reasons": [],
    }
    r = tools.save_draft(ctx, draft)
    assert r["saved"] is False
    assert r["draft_status"] == "none"
    assert r["blocked"] is True
    assert ctx.save_backend.list_drafts() == []


def test_save_draft_rejects_missing_date(tmp_path):
    """草稿缺日期时拒绝保存，绝不落盘。"""
    ctx = build_ctx(data_dir=DATA_DIR, drafts_file=str(tmp_path / "drafts.json"))
    draft = {
        "material_id": "M02",
        "date": None,  # 漏掉日期
        "time": "10:00",
        "review_status": "approved",
        "pending_confirmations": [],
        "risk_reasons": [],
    }
    r = tools.save_draft(ctx, draft)
    assert r["saved"] is False
    assert r["draft_status"] == "none"
    assert r["invalid"] is True
    assert ctx.save_backend.list_drafts() == []


def test_save_draft_rejects_blocked_material(tmp_path):
    """审核阻断不得被保存绕过：命中 block 规则的素材拒绝落盘。"""
    ctx = build_ctx(data_dir=DATA_DIR, drafts_file=str(tmp_path / "drafts.json"))
    draft = {
        "material_id": "M04",  # 敏感内容，命中 R1 block
        "date": "2026-10-28",
        "time": "10:00",
        "review_status": "approved",
        "pending_confirmations": [],
        "risk_reasons": [],
    }
    r = tools.save_draft(ctx, draft)
    assert r["saved"] is False
    assert r["draft_status"] == "none"
    assert r["blocked"] is True
    assert ctx.save_backend.list_drafts() == []


def test_save_draft_rejects_missing_info_material(tmp_path):
    """素材信息缺失不得被保存绕过：缺必填字段的素材拒绝落盘。"""
    ctx = build_ctx(data_dir=DATA_DIR, drafts_file=str(tmp_path / "drafts.json"))
    draft = {
        "material_id": "M03",  # 视频缺时长
        "date": "2026-10-28",
        "time": "11:00",
        "review_status": "approved",
        "pending_confirmations": [],
        "risk_reasons": [],
    }
    r = tools.save_draft(ctx, draft)
    assert r["saved"] is False
    assert r["draft_status"] == "none"
    assert r["invalid"] is True
    assert ctx.save_backend.list_drafts() == []


def test_save_draft_allows_hard_target_match(tmp_path):
    """硬性时段被满足时正常保存。"""
    ctx = build_ctx(
        data_dir=DATA_DIR,
        drafts_file=str(tmp_path / "drafts.json"),
        hard_targets={"M02": {"date": "2026-10-28", "time": "10:00"}},
    )
    draft = {
        "material_id": "M02",
        "date": "2026-10-28",
        "time": "10:00",
        "review_status": "approved",
        "pending_confirmations": [],
        "risk_reasons": [],
    }
    r = tools.save_draft(ctx, draft)
    assert r["saved"] is True
    assert r["draft_status"] == "saved"
    assert len(ctx.save_backend.list_drafts()) == 1

"""守卫层单元测试：硬性要求边界。"""
from src.guardrails import apply_guardrails


def test_hard_requirement_cannot_be_silently_changed():
    request = {
        "materials": [
            {"material_id": "M05", "target_time": "18:00", "requirements": [{"kind": "hard", "text": "必须 18:00"}]}
        ]
    }
    result = {
        "results": [
            {
                "material_id": "M05",
                "review_status": "approved",
                "suggested_slot": {"date": "2026-10-28", "time": "19:00"},
                "pending_confirmations": [],
            }
        ]
    }
    out = apply_guardrails(result, request)
    assert out["results"][0]["review_status"] == "blocked"
    assert any("未经用户同意" in p for p in out["results"][0]["pending_confirmations"])


def test_hard_requirement_met_is_untouched():
    request = {
        "materials": [
            {"material_id": "M05", "target_time": "18:00", "requirements": [{"kind": "hard", "text": "必须 18:00"}]}
        ]
    }
    result = {
        "results": [
            {
                "material_id": "M05",
                "review_status": "approved",
                "suggested_slot": {"date": "2026-10-28", "time": "18:00"},
                "pending_confirmations": [],
            }
        ]
    }
    out = apply_guardrails(result, request)
    assert out["results"][0]["review_status"] == "approved"


def test_hard_requirement_date_change_is_blocked():
    """硬性要求同时绑定日期：改到次日、时刻不变，守卫须更正为 blocked。"""
    request = {
        "date": "2026-10-28",
        "materials": [
            {"material_id": "M05", "target_time": "18:00", "requirements": [{"kind": "hard", "text": "必须 18:00"}]}
        ],
    }
    result = {
        "results": [
            {
                "material_id": "M05",
                "review_status": "approved",
                "suggested_slot": {"date": "2026-10-29", "time": "18:00"},
                "pending_confirmations": [],
            }
        ]
    }
    out = apply_guardrails(result, request)
    r = out["results"][0]
    assert r["review_status"] == "blocked"
    assert r["suggested_slot"] is None
    assert any("未经用户同意" in p for p in r["pending_confirmations"])


def test_reconcile_false_save_with_actual_failure():
    """模型虚报 saved，但工具实际返回 failed：守卫须对照实际结果更正。"""
    request = {
        "materials": [
            {"material_id": "M02", "target_time": "10:00", "requirements": [{"kind": "soft", "text": "倾向 10:00"}]}
        ]
    }
    result = {
        "results": [
            {
                "material_id": "M02",
                "review_status": "approved",
                "suggested_slot": {"date": "2026-10-28", "time": "10:00"},
                "draft_status": "saved",
            }
        ]
    }
    actual = {"M02": {"draft_status": "failed", "draft_id": None, "error": "保存后端异常，草稿未写入"}}
    out = apply_guardrails(result, request, actual_saves=actual)
    r = out["results"][0]
    assert r["draft_status"] == "failed"
    assert r["error"] == "保存后端异常，草稿未写入"

"""Shared validation for user requests, model arguments and saved slots."""
from datetime import date, datetime
import re


def validate_date(value) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("日期必须为 YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise ValueError("日期不存在") from None


def validate_time(value) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"\d{2}:\d{2}", value):
        raise ValueError("时刻必须为 HH:MM")
    try:
        datetime.strptime(value, "%H:%M")
    except ValueError:
        raise ValueError("时刻不存在") from None


def validate_request(request, max_steps=12) -> None:
    if not isinstance(max_steps, int) or isinstance(max_steps, bool) or max_steps < 0:
        raise ValueError("执行步数必须为非负整数")
    if not isinstance(request, dict) or not isinstance(request.get("materials"), list):
        raise ValueError("materials 必须为列表")
    if request.get("date") is not None:
        validate_date(request["date"])
    seen = set()
    for item in request["materials"]:
        if not isinstance(item, dict):
            raise ValueError("每项素材必须为对象")
        mid = item.get("material_id")
        if not isinstance(mid, str) or not mid.strip() or mid in seen:
            raise ValueError("素材 ID 必须为非空字符串，且请求内不得重复")
        seen.add(mid)
        validate_date(item.get("date") or request.get("date"))
        validate_time(item.get("target_time"))
        reqs = item.get("requirements", [])
        if not isinstance(reqs, list) or any(
            not isinstance(r, dict) or r.get("kind") not in ("hard", "soft") for r in reqs
        ):
            raise ValueError("requirements 必须为包含 hard/soft 类型的对象列表")

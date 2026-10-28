"""核心数据结构定义。

本模块只定义类型与 JSON 互转，不包含业务逻辑，便于各模块共享同一套结构。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


# 审核状态枚举（评审维度之一）
REVIEW_APPROVED = "approved"
REVIEW_BLOCKED = "blocked"
REVIEW_PENDING = "pending_confirmation"
REVIEW_INFO_MISSING = "info_missing"

# 草稿保存状态枚举
DRAFT_SAVED = "saved"
DRAFT_DUPLICATE = "duplicate"
DRAFT_FAILED = "failed"
DRAFT_NONE = "none"


@dataclass
class Material:
    """一条待审核素材的元数据。"""

    id: str
    title: str
    content_type: str
    category: str
    author: str
    duration_minutes: Optional[int] = None

    @classmethod
    def from_dict(cls, d: dict) -> "Material":
        keys = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: d.get(k) for k in keys})

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "content_type": self.content_type,
            "category": self.category,
            "author": self.author,
            "duration_minutes": self.duration_minutes,
        }

    def missing_fields(self) -> list[str]:
        """返回缺失的必填字段。

        - 所有素材都要求有 title/content_type/category/author；
        - 视频素材额外要求有 duration_minutes。
        """
        missing: list[str] = []
        for name in ("title", "content_type", "category", "author"):
            if not getattr(self, name, None):
                missing.append(name)
        if self.content_type == "video" and self.duration_minutes is None:
            missing.append("duration_minutes")
        return missing


@dataclass
class Rule:
    """一条审核规则（数据驱动，定义在 data/rules.json）。"""

    id: str
    name: str
    when: dict
    severity: str
    action: str
    message: str

    @classmethod
    def from_dict(cls, d: dict) -> "Rule":
        return cls(
            id=d["id"],
            name=d["name"],
            when=d["when"],
            severity=d.get("severity", "medium"),
            action=d.get("action", "warn"),
            message=d.get("message", ""),
        )


@dataclass
class RuleResult:
    """一条规则对一条素材的检查结果。"""

    rule_id: str
    name: str
    passed: bool
    severity: str
    action: str
    message: str

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "name": self.name,
            "passed": self.passed,
            "severity": self.severity,
            "action": self.action,
            "message": self.message,
        }


@dataclass
class Slot:
    """某个日期的某个时段占用情况。"""

    time: str
    status: str  # free / occupied
    occupied_by: Optional[str] = None

    def to_dict(self) -> dict:
        d: dict[str, Any] = {"time": self.time, "status": self.status}
        if self.occupied_by:
            d["occupied_by"] = self.occupied_by
        return d


@dataclass
class MaterialResult:
    """单个素材的最终排期结果（交付物）。"""

    material_id: str
    review_status: str
    risk_reasons: list[str] = field(default_factory=list)
    suggested_slot: Optional[dict] = None
    pending_confirmations: list[str] = field(default_factory=list)
    draft_status: str = DRAFT_NONE
    draft_id: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "material_id": self.material_id,
            "review_status": self.review_status,
            "risk_reasons": self.risk_reasons,
            "suggested_slot": self.suggested_slot,
            "pending_confirmations": self.pending_confirmations,
            "draft_status": self.draft_status,
            "draft_id": self.draft_id,
            "error": self.error,
        }

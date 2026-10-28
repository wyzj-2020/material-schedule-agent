"""规则引擎：把 data/rules.json 中的规则对素材元数据进行求值。

规则通过 `when` 字段用一个小型 DSL 描述匹配条件，本模块负责解释该 DSL。
支持的 `when` 键：
  - {"field": value}            素材字段等于给定值
  - {"duration_missing": true}  视频时长缺失
  - {"duration_gte": n}         时长大于等于 n 分钟
  - {"author_not_in": [...]}    作者不在给定列表
"""
from __future__ import annotations

from .schemas import Material, Rule, RuleResult


def _matches(when: dict, material: Material) -> bool:
    """判断一条规则的条件是否命中素材（when 中多个键为 AND 关系）。"""
    for key, value in when.items():
        if key == "duration_missing":
            if (material.duration_minutes is None) != value:
                return False
        elif key == "duration_gte":
            if material.duration_minutes is None or material.duration_minutes < value:
                return False
        elif key == "author_not_in":
            if material.author in value:
                return False
        else:
            if getattr(material, key, None) != value:
                return False
    return True


def check_rules(material: Material, rules: list[Rule]) -> list[RuleResult]:
    """对单条素材执行全部规则，返回结果列表（含未命中的规则）。"""
    results: list[RuleResult] = []
    for rule in rules:
        hit = _matches(rule.when, material)
        results.append(
            RuleResult(
                rule_id=rule.id,
                name=rule.name,
                passed=not hit,
                severity=rule.severity,
                action=rule.action if hit else "pass",
                message=rule.message if hit else "",
            )
        )
    return results

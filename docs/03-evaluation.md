# 03 评估

## 1. 测试预期

| 验收点 | 测试函数 | 预期 |
|---|---|---|
| 正常排期 | `test_normal_schedule_and_time_conflict` | M02 通过并保存 |
| 时段冲突 | 同上 | M01 建议 19:00 并待确认 |
| 规则风险 | `test_rule_risk` | M04 阻断并给出敏感原因 |
| 规则需确认（confirm） | `test_confirm_rule_requires_confirmation` | R3 命中后空闲时段也待确认，不直接放行 |
| 素材信息缺失 | `test_missing_info` | M03 标记 info_missing |
| 硬性确认边界 | `test_hard_requirement_boundary` | M05 冲突时阻断，待确认 |
| 硬性要求前置校验（保存前拒绝） | `test_hard_requirement_rejected_at_save_boundary` | 模型把硬性时段改存他处被 save_draft 拒绝，不落盘 |
| 硬性要求绑定日期（保存前拒绝） | `test_hard_requirement_date_change_rejected_at_save_boundary` | 改到次日、时刻不变也被拒绝，不落盘 |
| 草稿字段校验（漏日期） | `test_save_draft_rejects_missing_date` | 缺日期草稿拒绝保存，不落盘 |
| 审核阻断保存前拒绝 | `test_save_draft_rejects_blocked_material` | 命中 block 规则的素材被 save_draft 拒绝，不落盘 |
| 信息缺失保存前拒绝 | `test_save_draft_rejects_missing_info_material` | 缺必填字段的素材被 save_draft 拒绝，不落盘 |
| 审核阻断事后纠正 | `test_blocked_material_cannot_be_reported_approved` | 模型绕过审核虚报 approved 被更正为 blocked，不落盘 |
| 报告时段对照 | `test_reported_slot_must_match_actual_draft` | 保存 10:00 却报告 11:00，最终以真实草稿时段 10:00 为准 |
| 重复提交 | `test_duplicate_submission` | 二次保存 duplicate，仅一份草稿 |
| 重复提交（措辞无关） | `test_save_draft_dedup_ignores_confirmation_wording` | 同一素材/日期/时段，仅待确认文本换说法仍 duplicate，仅一份草稿 |
| 保存失败 | `test_save_failure` | draft_status=failed + error |
| 保存失败（落盘回滚） | `test_save_rollback_on_persist_failure` | 落盘失败不污染内存，二次保存不误报 duplicate |
| 保存失败（防虚报） | `test_false_save_report_is_corrected` | 模型虚报 saved 被守卫更正为 none |
| 步数上限 | `test_step_limit_returns_unfinished` | unfinished=true |
| 步数上限（局部结果） | `test_step_limit_carries_partial_results` | unfinished=true 且携带已完成素材结果 |
| 步数上限（保存边界） | `test_step_limit_at_save_boundary_carries_saved_draft` | 保存恰好耗尽步数时，已落盘草稿仍进入局部结果 |
| 步数上限（保存失败边界） | `test_step_limit_at_save_boundary_carries_failed_draft` | 持续失败耗尽步数时，携带 failed 与原因 |
| 组合边界（硬性拒绝 + 步数上限） | `test_hard_rejection_combined_with_step_limit` | 局部结果须为 blocked，而非模型声称的 approved |
| 批量无冲突 | `test_batch_scheduling_no_conflict` | 同批多素材不占用同一时段 |
| 跨运行无冲突（单独） | `test_cross_run_separate_submission_no_conflict` | 已保存草稿在后续运行中被视为占用 |
| 跨运行无冲突（批量） | `test_batch_after_existing_single_draft` | 已有草稿去重 + 后续批量避开占用 |
| 动态决策路径 | `test_dynamic_decision_paths` | 冲突→补查→替代；风险→阻断（不再查时段） |

## 2. 实际结果

单元/验收测试及独立回归：**121 passed, 0 failed**（2026-10-29，`python -m pytest tests/ -q --junitxml examples/test-results.xml`）。原始结果：[examples/test-results.xml](../examples/test-results.xml)。

在原有 110 项基础上增加 11 项提交回归：CLI 环境配置与显式参数、强制真实模式、模型事实摘要、工具 schema、私有续接字段与多工具 ID、硬性冲突空原因补齐、两种合法批量分配及冲突拒绝、提交包排除密钥和泄露检测。

本轮将上一轮 59 项独立回归原样纳入 `tests/test_regressions.py`，此前 16 个失败用例全部通过；另补充 `tests/test_recovery.py` 的 6 项测试。覆盖缺字段结果纠正、非法日期/时刻、请求范围、待确认提示、非法工具调用、模型超时、异常后恢复，以及两个独立进程交错保存。不同槽位两份草稿均保留；同槽位只允许一份成功，无覆盖和重复 ID。全部使用临时草稿文件，不改原始业务数据。

示例运行记录（`examples/`）：

| 输入 | 输出 | Trace | 覆盖 |
|---|---|---|---|
| `input_normal.json` | `output_normal.json` | `trace_normal.json` | 正常排期 + 时段冲突 |
| `input_batch.json` | `output_batch.json` | `trace_batch.json` | 批量排期无冲突 |
| `input_normal.json`（二次运行） | `output_normal_rerun.json` | `trace_normal_rerun.json` | 重复提交 |
| `input_rule_risk.json` | `output_rule_risk.json` | `trace_rule_risk.json` | 规则风险 |
| `input_missing_info.json` | `output_missing_info.json` | `trace_missing_info.json` | 信息缺失 |
| `input_hard_requirement.json` | `output_hard_requirement.json` | `trace_hard_requirement.json` | 硬性确认边界 |
| `input_save_fail.json`（--fail-save） | `output_save_fail.json` | `trace_save_fail.json` | 保存失败（工具失败） |

### 关键结果摘录

- **正常排期 + 时段冲突**（`output_normal.json`）：M01 `review_status=pending_confirmation`、`suggested_slot=19:00`、待确认项「目标时段 18:00 被占用，建议调整为 19:00，请确认」；M02 `approved`、10:00。二者均 `draft_status=saved`。**未出现"已发布"字样**。
- **批量排期无冲突**（`output_batch.json`）：M01（目标 18:00）先落到 19:00 并保存，其草稿在本批内即视为占用；M02（目标 19:00）随后查询时 19:00 返回 `occupied_by=M01`，自动落到 21:00。二者时段互不重叠（19:00 / 21:00），均 `pending_confirmation + saved`。`trace_batch.json` 中可见 M02 的 `query_slots(19:00)` 结果为 `occupied`。
- **重复提交**（`output_normal_rerun.json`）：两项均为 `draft_status=duplicate`，`draft_id` 与首次一致（D001/D002）；`data/drafts.json` 仍只有 2 份草稿。
- **保存失败**（`output_save_fail.json`）：M02 `draft_status=failed`、`error="保存后端异常，草稿未写入"`。
- **保存失败防虚报**：`save` 先落盘再更新内存，落盘失败即回滚（二次保存不会误报 `duplicate`）；落盘采用「同目录临时文件 + 原子替换」，写入中断不破坏已有数据；守卫对照工具观察记录，模型虚报的 `saved` 会被更正为 `none`/`failed`。
- **硬性要求保存前校验**：`save_draft` 在落盘前即拒绝把硬性发布时段（日期或时刻任一）改动的请求（返回 `blocked`、`draft_status=none`），违反硬性边界的草稿不会进入磁盘；同时拒绝缺少日期/时段的草稿；事后守卫仍保留，作为第二道防线。
- **审核阻断保存前校验**：`save_draft` 在落盘前即拒绝「素材不存在 / 信息缺失 / 命中 block 规则」的草稿，阻断素材无法绕过审核落盘；事后守卫独立重算审核结论，模型虚报为 approved/pending 的结果被强制更正为 blocked/info_missing 并清空时段。
- **报告时段对照**：守卫对照工具实际落盘结果，成功落盘的素材其 `suggested_slot` 强制以真实草稿时段为准，防止「保存 10:00 却报告 11:00」的虚报。

### 两条决策路径（体现模型依据不同工具结果调整动作）

- **路径 A（冲突→替代）** `trace_normal.json` M01：`read_material → check_rule → query_slots(18:00, occupied) → query_slots(全天, 找到 19:00) → save_draft(19:00, pending_confirmation)`。决策依据逐条记录，如「目标时段 18:00 被 M99 占用，扩大范围查询可用时段」「18:00 被占用，找到最近空闲 19:00，建议替代并标记待确认」。
- **路径 B（风险→阻断）** `trace_rule_risk.json` M04：`read_material → check_rule(R1 命中阻断) → finish(blocked)`。**未调用 query_slots**——模型据规则结果直接阻断。

## 3. 当前版本真实模型验证（2026-10-29）

本轮实际调用本地配置的 `deepseek-v4-flash`，**7 组业务结果检查通过**。完整汇总见 [summary.json](../examples/submission-real-20261029-release/summary.json)。该结果验证以下有限样本，并不代表模型在任意输入下都能遵循策略。

| 场景 | 当前实际结果 | 输入 / 输出 / Trace |
|---|---|---|
| 正常与冲突 | M01 改建议 19:00、待确认；M02 10:00；草稿保存成功 | [记录](../examples/submission-real-20261029-release/normal/) |
| 重复提交 | 两项 duplicate，复用已有草稿 | [记录](../examples/submission-real-20261029-release/normal_rerun/) |
| 规则风险 | M04 blocked，未保存 | [记录](../examples/submission-real-20261029-release/rule_risk/) |
| 信息缺失 | M03 检出缺视频时长，未保存 | [记录](../examples/submission-real-20261029-release/missing_info/) |
| 硬性冲突 | M05 blocked、草稿 none；工具拒绝未经确认的改时刻请求 | [记录](../examples/submission-real-20261029-release/hard_requirement/) |
| 保存失败 | 两次保存失败，最终 failed 并保留原因 | [记录](../examples/submission-real-20261029-release/save_fail/) |
| 批量排期 | M01 21:00 待确认，M02 保留目标 19:00；无冲突 | [记录](../examples/submission-real-20261029-release/batch/) |

每组目录包含 `input.json`、`output.json`、`trace.json`；Trace 记录步骤、业务工具参数、返回结果、后续动作，以及模型提供的简短事实摘要，最终收尾也有独立摘要。私有推理续接字段只在接口调用内存中保留，不提交。

本轮记录的源码 SHA-256 为 `253e6e74f906af59666f9ec0736fa7c30ed7672959c1c3fc8cfb1ae2cc41e0d0`，已与当前 `src/*.py` 核对一致。计算方式为按文件名排序，依次将文件名 UTF-8 字节和内容加入 SHA-256。模型名、接口地址和 UTC 时间均写入 `meta`，不是凭历史文件推断。

两条真实调整路径：

- 正常场景中，模型读到 18:00 占用后补查替代时段，选择 19:00 并待确认保存；M02 保留空闲的 10:00。
- 硬性场景中，模型查到 18:00 占用后尝试存 19:00，保存工具拒绝；模型观察到拒绝结果后以 blocked、none 收尾并请求确认。这证明工具结果改变了后续动作，也暴露了模型并非始终自行遵循硬性策略，系统依赖保存前守卫保障边界。

批量场景的合法分配不唯一。真实模型给 M02 保留目标 19:00，将 M01 放到 21:00；Mock 则按输入顺序先给 M01 19:00，再给 M02 21:00。验证脚本最初把后者当作唯一答案，导致误判；已改为检查无冲突、已有占用、变更待确认和实际保存状态。原始检查结果保存在 `summary.initial.json`，输出和 Trace 未改写。

本轮还保留了三个早期尝试目录作为失败证据：`submission-real-20261029`（沙箱 ProxyError）、`submission-real-20261029-verified`（首轮工具调用后 HTTPError）、`submission-real-20261029-final`（硬性冲突已阻断，但结构化风险/确认字段为空）。随后补齐接口续接和结果边界，重新运行生成上表当前证据；未将这些失败记录改名为成功。

复现本轮验证：`python scripts/verify_real.py --output-dir examples/new-real-verification`。输出目录须不存在；`--check-existing` 只复核已有记录，不调用模型。

### 历史真实模型记录

以下为仓库此前保留的历史结果；这些旧 Trace 缺少本轮源码指纹，不能替代上节 2026-10-29 当前版本的真实验证：

| 输入 | 结果 | 关键行为 |
|---|---|---|
| `input_normal` | M01 `pending_confirmation`@19:00、M02 `approved`@10:00 | 冲突 → 补查 → 替代 → 待确认 |
| `input_hard_requirement` | M05 `blocked`、`suggested_slot=null` | 硬性冲突未改时段、未存草稿，请求确认 |
| `input_rule_risk` | M04 `blocked`（敏感内容） | 命中 R1 直接阻断 |
| `input_missing_info` | M03 `blocked`（缺少时长） | 命中 R2，未排期 |
| `input_save_fail`（--fail-save） | M02 `approved`、`draft_status=failed` | 重试一次后如实上报失败 |
| `input_batch` | M01 `pending_confirmation`@19:00、M02 `pending_confirmation`@21:00 | 批量多素材互不占用同一时段 |

真实模型 Trace 见 `examples/trace_*_real.json`。示例——硬性冲突路径的完整工具序列：

```
read_material → check_rule → query_slots(18:00) → query_slots(全天补查) → finish(blocked)
```

模型发现 18:00 被占后，因是硬性要求**未改时段、未存草稿**，而是补查全天以向用户提供备选（19:00/21:00），最终阻断并请求确认。

与 Mock 的两点差异（均为真实模型自主决策，非缺陷）：

- M03 信息缺失被模型归类为 `blocked`（经规则 R2），而 Mock 归类为 `info_missing`（经 read_material 缺失字段）；两者都正确检测并阻断，仅分类路径不同。
- 硬性冲突时模型多查了一次全天时段以向用户提供备选，Mock 则直接阻断。

## 4. 失败与限制

- **验证边界**：121 项自动化测试使用 Mock、模拟接口和故障注入；另有上节 7 组真实模型样本。二者分开记录，不用离线测试数证明真实模型质量。
- **真实模型局限**：硬性冲突时出现过越界保存尝试，已被工具拦截；规则阻断场景也额外查询了时段，存在冗余调用。当前结果说明系统边界有效，不代表模型所有中间动作都最优或合规。
- 排期按「小时粒度、单日期」简化，未处理跨日、时长占用多个时段等复杂情况。
- 保存后端使用本地 JSON 文件，已加入锁内重读、冲突校验与原子替换；本地 Windows 多进程已测试。未验证网络文件系统、跨机器共享存储或断电恢复，不能替代数据库的完整事务保证。
- 规则 DSL 仅覆盖示例所需的条件类型，扩展需增加条件求值分支。

## 5. 验证材料引用

运行记录见 `examples/trace_*.json`（含步骤、工具参数、返回摘要、后续动作、决策依据）；对应测试见 `tests/test_acceptance.py`。

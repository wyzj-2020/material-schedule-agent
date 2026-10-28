# 02 方案设计

## 1. 总体架构

```
输入请求 → 约束解析(硬/软要求) → ReAct 循环(模型决策 + 工具观察) → 守卫校验 → 结构化结果 + Trace
```

组件：

| 模块 | 职责 |
|---|---|
| `src/agent.py` | ReAct 循环、步数上限 |
| `src/llm.py` | 可插拔决策引擎：MockLLM（离线策略）+ OpenAICompatLLM（真实模型） |
| `src/tools.py` | 4 个业务工具 + 草稿保存后端（去重/持久化/失败注入） |
| `src/rules.py` | 数据驱动的规则求值引擎 |
| `src/guardrails.py` | 硬性要求边界兜底 |
| `src/tracer.py` | 记录步骤、工具参数、返回摘要、后续动作、决策依据 |
| `src/pipeline.py` | 装配（加载数据、构建上下文、选择引擎） |
| `src/main.py` | CLI 入口 |
| `src/validation.py` | 请求结构、日期及时刻校验 |
| `src/file_lock.py` | 跨进程文件锁，限定等待时间 |

## 2. ReAct 循环与动态决策

循环逻辑（`src/agent.py`）：

```
while 步数 < max_steps:
    decision = 决策引擎.decide(context)   # ToolCall 或 Final
    if Final: 守卫校验 → 输出结果
    else:     执行工具 → 观察结果 → 追加进 context（并记录 Trace）
超上限 → 输出 unfinished（含已执行步数）
```

**预设流程只做骨架**（写进 system prompt / Mock 策略），**决策点由模型依据工具返回结果做出**：

| 观察到的工具结果 | 模型可选动作 | 对应验收点 |
|---|---|---|
| 素材信息缺失 | 补查 / 请求补充 / 阻断 | 信息缺失 |
| 规则命中风险 | 补查 / 请求确认 / 阻断 | 规则风险 |
| 目标时段被占用 | 补查附近时段 / 替代+待确认 / 请求确认 / 阻断 | 时段冲突 |
| 保存失败 | 重试一次 / 上报失败 | 保存失败 |
| 相同请求再次出现 | 识别为重复 → 返回既有草稿 | 重复提交 |

**硬/软要求区分**：

- 硬性（`kind=hard`）：冲突时只能请求确认或阻断，不得自动改时段。
- 软性（`kind=soft`）：可给替代时段并标记 `pending_confirmation`。

## 3. 工具设计

| 工具 | 输入 | 返回 |
|---|---|---|
| `read_material` | material_id | 素材记录 / 不存在 / 缺失字段 |
| `check_rule` | material_id | 各规则命中情况 + any_block + 风险原因 |
| `query_slots` | material_id, date, [start, end] | 时段占用/空闲列表 + 空闲时段 |
| `save_draft` | 草稿内容 | saved / duplicate / failed + draft_id |

## 4. 关键机制

1. **硬性要求边界**：`tools.save_draft` 在保存前即校验，硬性发布时段（日期或时刻任一）被改的请求直接拒绝、不落盘；`guardrails.apply_guardrails` 再对最终结果兜底——若硬性时段被改且状态仍为 approved，强制改为 blocked。两道防线保证边界不可突破。
2. **审核阻断边界**：`tools.save_draft` 在保存前即拒绝「素材不存在 / 信息缺失 / 命中 block 规则」的草稿，防止模型绕过审核把应阻断素材存成 approved/pending；`guardrails` 事后独立重算审核结论，若最终结果仍虚报为 approved/pending，强制更正为 blocked/info_missing 并清空时段。
3. **防重复**：草稿去重键 = hash(素材ID, 日期, 时段)（稳定请求标识，不含 `review_status` / `pending_confirmations` 等模型措辞）；相同请求返回既有 `draft_id` 且 `duplicate=true`，不新增草稿。
4. **报告时段对照**：`guardrails` 对照工具实际落盘结果，成功落盘的素材其 `suggested_slot` 强制以真实草稿时段为准，防止模型虚报时段与草稿不一致。
5. **只存草稿**：系统不存在发布工具，输出状态只可能是 saved/duplicate/failed/none。
6. **保存失败**：工具返回 `saved=false + error`，Agent 如实上报 `draft_status=failed`；Mock 策略会先重试一次，仍失败则上报。
7. **步数上限**：`max_steps`（默认 12）用尽后返回 `unfinished=true` + 原因；并合并「决策引擎已收尾的素材」与「工具层已执行 save_draft 的素材」作为局部结果，再统一过硬性/审核边界与草稿状态对照，避免保存动作恰好耗尽步数时丢失已落盘草稿或阻断信号。
8. **规则动作语义**：`block` → 阻断；`confirm` → 标记 `pending_confirmation` 并把规则原因放入 `pending_confirmations`（即便时段空闲也不放行）；`warn` → 记入 `risk_reasons`，仍可 approved。

## 5. 技术取舍

| 取舍 | 选择 | 理由 |
|---|---|---|
| Agent 框架 | **原生手写 ReAct**（非 LangGraph/ADK） | 题目明说框架不加分；原生实现可完全掌控循环、Trace、守卫，便于讲清取舍 |
| 模型 | DeepSeek（OpenAI 兼容）+ 离线 MockLLM | 成本低；离线 Mock 保证无网可复现，且 Mock 明确只验证流程 |
| 规则 | 数据驱动（`data/rules.json` + 小型 DSL） | 评审规则可改数据不改代码 |
| 数据 | 本地 JSON | 无需 DB，评测材料即数据 |
| 运行 | CLI + JSON 输出 + `--trace` | 命令可复制，Trace 可直接评审 |

**离线 Mock 的定位**：MockLLM 是读取工具观察结果的确定性分支策略，用于流程回归；它不属于真实模型决策，也不证明满足真实模型 ReAct 效果。真实模型路径由 `OpenAICompatLLM` 承担，需配置 API Key 并独立验证。

## 6. 回归修复后的边界

- 保存前强制检查请求范围、有效日期/时刻、素材完整性、阻断及确认规则、硬性要求和时段占用；确认规则即使被模型忽略，也会把草稿改为待确认。
- 正常结束和步数耗尽共用边界校验；缺失日期/时刻不能被报告为 approved。拒绝原因进入风险原因，待确认结果必须包含确认提示。
- 最终时段、审核状态及待确认项以工具返回的实际草稿为准；重复保存返回原草稿内容，避免措辞变化造成报告与存储不一致。
- 每次保存获取线程锁和操作系统文件锁，锁内重新读取、去重、检查冲突、写临时文件并原子替换。操作系统文件锁默认最多等待 5 秒，失败明确上报；本地 Windows 多进程用例已验证。
- 非法工具调用转为错误观察供模型纠正；模型超时/协议异常返回 unfinished，并保留已保存的局部结果。已有草稿损坏时停止任务并保留原文件，避免覆盖。

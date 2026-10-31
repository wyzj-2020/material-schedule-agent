# 素材审核与发布排期 Agent

帮助内容团队找出待处理素材，形成**无冲突、可确认**的发布排期。系统只保存草稿，**不接真实发布**。

GitHub 仓库：[wyzj-2020/material-schedule-agent](https://github.com/wyzj-2020/material-schedule-agent)。

## 核心特性

- **ReAct 工具调用循环**：模型选动作 → 执行工具 → 观察结果 → 调整下一步或结束。
- **4 个业务工具**：素材读取、规则检查、时段查询、排期草稿保存（另加 `finish` 作为终止控制）。
- **硬性要求边界**：未经用户同意，不得擅自改变硬性发布要求（冲突时只能请求确认或阻断；保存前即校验，违反的草稿不落盘）。
- **防重复**：相同请求重复保存只产生一份草稿。
- **保存失败如实上报**：落盘失败自动回滚、模型虚报 success 会被守卫对照工具观察记录更正，绝不虚报成功。
- **执行步数上限**：达到上限返回明确的未完成状态，并携带已完成的局部结果。
- **可插拔决策引擎**：离线 Mock（默认，无第三方依赖）+ 真实模型（DeepSeek 等 OpenAI 兼容接口）。

## 目录结构

```
material-schedule-agent/
├── README.md
├── requirements.txt
├── .env.example
├── src/            # 项目代码（agent/tools/llm/guardrails/tracer/pipeline/main）
├── tests/          # 可执行测试（pytest）
├── examples/       # 输入、实际输出、Trace
├── data/           # 合成素材、规则、已有排期、草稿
└── docs/           # 需求/方案/评估/AI 使用/复盘
```

## 环境要求

- Python 3.11+
- 离线 Mock 模式：**无需任何第三方库**
- 真实模型模式：需要 `requests`（`pip install -r requirements.txt`）

## 安装

```bash
pip install -r requirements.txt
```

## 运行方式（CLI）

在项目根目录运行。显式使用 `--mock`，即使本地已有密钥，也不会调用网络。以下单行命令适用于 PowerShell 和 Bash，各示例使用独立草稿文件：

```bash
# 正常排期 + 时段冲突
python -m src.main --mock --input examples/input_normal.json --drafts-file examples/review-normal-drafts.json --output examples/review-normal-output.json --trace examples/review-normal-trace.json

# 批量排期（无冲突）：M01 先占 19:00，M02 自动落到 21:00
# （使用独立草稿文件，避免与其它示例共享 data/drafts.json 相互影响）
python -m src.main --mock --input examples/input_batch.json --drafts-file examples/review-batch-drafts.json --output examples/review-batch-output.json --trace examples/review-batch-trace.json

# 规则风险
python -m src.main --mock --input examples/input_rule_risk.json --drafts-file examples/review-risk-drafts.json --trace examples/review-risk-trace.json

# 素材信息缺失
python -m src.main --mock --input examples/input_missing_info.json --drafts-file examples/review-missing-drafts.json --trace examples/review-missing-trace.json

# 硬性要求冲突（确认边界）
python -m src.main --mock --input examples/input_hard_requirement.json --drafts-file examples/review-hard-drafts.json --trace examples/review-hard-trace.json

# 保存失败（工具失败场景）
python -m src.main --mock --input examples/input_save_fail.json --fail-save --drafts-file examples/review-fail-drafts.json --trace examples/review-fail-trace.json

# 重复提交：再次运行相同请求，save_draft 返回 duplicate，不新增草稿
python -m src.main --mock --input examples/input_normal.json --drafts-file examples/review-normal-drafts.json --trace examples/review-normal-rerun-trace.json
```

每次运行会在终端打印结构化结果，并可选写入 `--output` / `--trace` 文件。

> Windows 传统控制台若中文显示乱码，可在运行前执行 `chcp 65001`（或 `set PYTHONIOENCODING=utf-8`）。输出到文件的结果始终为 UTF-8，不受影响。

### 真实模型模式（可选）

把密钥写入项目根目录 `.env`，不配置则自动用离线 Mock：

```bash
cp .env.example .env   # 然后填入 DEEPSEEK_API_KEY
python -m src.main --real --input examples/input_normal.json --drafts-file examples/review-real-drafts.json --trace examples/review-real-trace.json
```

`DEEPSEEK_MODEL` 与 `DEEPSEEK_BASE_URL` 均实际生效，也可用 `--model` / `--base-url` 覆盖。`--real` 缺少密钥会明确报错，不会降级为 Mock。

2026-10-29 使用本地配置的 DeepSeek `deepseek-v4-flash` 实际验证 **7 组**：正常、重复提交、规则风险、信息缺失、硬性冲突、保存失败、批量排期。当前证据见 [本轮真实模型汇总](examples/submission-real-20261029-release/summary.json)，每组包含输入、输出和 Trace；旧版 `*_real.json` 保留作为历史记录。

重新生成验证（发送合成资料，输出目录须不存在）：

```text
python scripts/verify_real.py --output-dir examples/new-real-verification
```

Trace 记录模型返回的简短事实摘要、后续动作、模型配置、UTC 时间和源码 SHA-256。接口需要的私有续接字段仅保留于内存，不进入公开 Trace。批量验收检查时段不冲突、改动待确认；不要求真实模型复制 Mock 的处理顺序。

## 测试

```bash
python -m pytest tests/ -v
```

覆盖：4 个业务工具、硬性要求守卫、6 个验收点、批量/跨运行无冲突、保存失败回滚与防虚报、两条动态决策路径、步数上限。

2026-10-29 完整回归：**121 passed, 0 failed**。`tests/test_regressions.py` 包含此前独立复测用例，`tests/test_recovery.py` 覆盖异常恢复和真实多进程写入，`tests/test_submission.py` 覆盖模型配置、事实摘要、接口续接与安全打包。原始结果见 [examples/test-results.xml](examples/test-results.xml)。

请求必须包含有效的日期（`YYYY-MM-DD`）、时刻（`HH:MM`）与非空素材 ID；素材级 `date` 可以覆盖请求日期。只允许保存本次请求内的素材。异常输入和模型超时返回 `unfinished=true`；CLI 对未完成任务返回退出码 2，审核阻断或明确保存失败本身可作为已完成的处理结果返回。

草稿写入使用同目录临时文件、原子替换和操作系统文件锁；锁内重读最新草稿并重新检查冲突，避免多实例覆盖或重复占用。旁边的 `.json.lock` 文件会保留以维持锁身份，运行时不要删除。

## 评审入口

- 需求与成功标准：[docs/01-requirements.md](docs/01-requirements.md)
- 方案与取舍：[docs/02-solution.md](docs/02-solution.md)
- 测试与评估：[docs/03-evaluation.md](docs/03-evaluation.md)
- AI 使用记录：[docs/04-ai-usage.md](docs/04-ai-usage.md)
- 复盘：[docs/05-retrospective.md](docs/05-retrospective.md)
- 运行记录：[examples/](examples/)（`trace_*.json` 含步骤、工具参数、返回摘要、后续动作、决策依据）
- 提交信息与待办：[SUBMISSION.md](SUBMISSION.md)（第 1 题、个人实际投入 4 小时；仓库已创建，最终 commit SHA 与评审权限待核验）

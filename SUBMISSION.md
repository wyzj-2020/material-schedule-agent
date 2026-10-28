# 提交信息

| 项目 | 内容 |
|---|---|
| 题号 | 第 1 题 |
| 题目 | 素材审核与发布排期 Agent |
| 实际投入时间 | 4 小时 |
| GitHub URL | 尚未创建；上传后填写真实仓库地址 |
| 最终 commit SHA | 最终提交前执行 `git rev-parse HEAD`，将结果随仓库地址提交 |
| 私有仓库访问 | 如设为私有，须向指定评审账号开放读取权限 |

## 评审步骤

在项目根目录执行以下命令。离线运行不需要密钥，命令适用于 PowerShell 和 Bash。

```text
python -m pip install -r requirements.txt
python -m pytest tests/ -q
python -m src.main --mock --input examples/input_normal.json --drafts-file examples/review-drafts.json --output examples/review-output.json --trace examples/review-trace.json
```

再次执行最后一条命令，可验证相同请求返回 duplicate，草稿不会重复创建。

真实模型验证：在本地 `.env` 配置密钥后，执行下列命令，输出目录必须尚不存在。

```text
python scripts/verify_real.py --output-dir examples/new-real-verification
```

历史记录与当前验证应分开阅读；以评估文档列出的最新源码指纹和实际运行结果为准。真实模型模式不会无提示地降级为 Mock。

## AI 与个人贡献

个人主导验收和迭代，Claude Code 与 Codex 辅助实现、测试和文档；详见 [AI 使用记录](docs/04-ai-usage.md)。个人贡献仅记录会话中可核实的行为，不将 AI 生成代码描述为本人独立手写。

## 未完成与提交前检查

- GitHub 仓库尚未创建，因此远程地址和评审权限尚不能核验。
- 将题号、真实 GitHub URL、最终 commit SHA 一起提交给评审；本文件中的命令不等于实际 SHA。
- 当前 121 项自动化测试通过、7 组真实模型业务检查通过；路径与局限见 `docs/03-evaluation.md`，失败尝试也已保留。
- 不上传 `.env`、缓存、虚拟环境或本地密钥。可执行 `python scripts/build_submission.py` 生成经过文件清单和密钥检查的 ZIP；GitHub 上传仍须检查实际暂存文件。
- 当前按离散时段处理，尚未支持跨时段时长占用、网络文件系统和断电恢复。

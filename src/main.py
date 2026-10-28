"""CLI 入口。

用法示例（离线 Mock）：
    python -m src.main --input examples/input_normal.json --trace examples/trace_normal.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .pipeline import load_env, run


def _ensure_utf8_stdio() -> None:
    """Windows 控制台默认 GBK，重配置为 UTF-8 以保证中文正常显示（失败则忽略）。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="素材审核与发布排期 Agent")
    p.add_argument("--input", required=True, help="请求 JSON 路径")
    p.add_argument("--output", help="最终结果输出路径（可选）")
    p.add_argument("--trace", help="Trace 输出路径（可选）")
    engine = p.add_mutually_exclusive_group()
    engine.add_argument("--mock", action="store_true", help="强制离线 Mock 模式")
    engine.add_argument("--real", action="store_true", help="强制真实模型模式；无密钥时明确报错")
    p.add_argument("--model", help="模型名称（覆盖 DEEPSEEK_MODEL）")
    p.add_argument("--base-url", help="兼容接口地址（覆盖 DEEPSEEK_BASE_URL）")
    p.add_argument("--max-steps", type=int, default=12, help="执行步数上限")
    p.add_argument("--fail-save", action="store_true", help="模拟保存失败")
    p.add_argument("--reset-drafts", action="store_true", help="运行前清空草稿")
    p.add_argument("--drafts-file", default=None, help="草稿持久化路径（默认 data/drafts.json）")
    args = p.parse_args(argv)

    _ensure_utf8_stdio()
    load_env()

    try:
        with open(args.input, "r", encoding="utf-8") as stream:
            request = json.load(stream)
    except (OSError, ValueError) as exc:
        p.error(f"无法读取请求文件：{exc}")

    if args.reset_drafts and args.drafts_file:
        with open(args.drafts_file, "w", encoding="utf-8") as f:
            json.dump([], f)

    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if args.real and not api_key:
        p.error("真实模型模式需要配置 DEEPSEEK_API_KEY")
    mode = "mock" if (args.mock or not api_key) else "real"
    model = args.model or os.environ.get("DEEPSEEK_MODEL") or "deepseek-chat"
    base_url = args.base_url or os.environ.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com"
    if mode == "mock":
        print("离线 Mock 模式（不调用真实模型，仅验证流程）", file=sys.stderr)

    result, tracer = run(
        request,
        mode=mode,
        max_steps=args.max_steps,
        fail_save=args.fail_save,
        drafts_file=args.drafts_file,
        api_key=api_key,
        model=model,
        base_url=base_url,
        meta={"input": args.input, "mode": mode, "max_steps": args.max_steps},
    )

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
    if args.trace:
        tracer.write(args.trace)

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if result.get("unfinished") else 0


if __name__ == "__main__":
    raise SystemExit(main())

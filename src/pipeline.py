"""流水线装配：加载数据、构建工具上下文、选择决策引擎并运行 Agent。

测试与 CLI 都通过 run() 复用同一套装配逻辑。
"""
from __future__ import annotations

import json
import os
import hashlib
from datetime import datetime, timezone

from .agent import run_agent
from .guardrails import hard_targets
from .llm import MockLLM, OpenAICompatLLM
from .schemas import Material, Rule
from .tools import SaveBackend, ToolContext
from .tracer import TraceLogger
from .validation import validate_request

DATA_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "data"))


def load_env(path: str | None = None) -> None:
    """从项目根目录 .env 加载环境变量（简易实现，无第三方依赖）。

    只设置尚未存在的变量，不覆盖已存在的环境变量；忽略空行与注释。
    """
    env_path = path or os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))
    if not os.path.exists(env_path):
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def load_json(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_ctx(
    data_dir: str = DATA_DIR,
    drafts_file: str | None = None,
    fail_save: bool = False,
    hard_targets: dict | None = None,
) -> ToolContext:
    materials = {mid: Material.from_dict(d) for mid, d in load_json(os.path.join(data_dir, "materials.json")).items()}
    rules = [Rule.from_dict(r) for r in load_json(os.path.join(data_dir, "rules.json"))]
    schedule = load_json(os.path.join(data_dir, "existing_schedule.json"))
    save_backend = SaveBackend(
        drafts_file=drafts_file or os.path.join(data_dir, "drafts.json"),
        force_fail=fail_save,
    )
    return ToolContext(materials, rules, schedule, save_backend, hard_targets=hard_targets or {})


def run(
    request: dict,
    *,
    mode: str = "mock",
    max_steps: int = 12,
    fail_save: bool = False,
    data_dir: str = DATA_DIR,
    drafts_file: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    meta: dict | None = None,
) -> tuple[dict, TraceLogger]:
    """运行一次排期任务，返回 (最终结果, TraceLogger)。"""
    tracer = TraceLogger(meta=meta or {"mode": mode})
    try:
        validate_request(request, max_steps)
    except ValueError as exc:
        result = {"results": [], "summary": {"total": 0}, "unfinished": True,
                  "reason": f"请求无效：{exc}"}
        tracer.finish(result, unfinished=True)
        return result, tracer
    load_env()
    if mode not in ("mock", "real"):
        result = {"results": [], "summary": {"total": 0}, "unfinished": True, "reason": "未知决策模式"}
        tracer.finish(result, unfinished=True)
        return result, tracer
    model = model or os.environ.get("DEEPSEEK_MODEL") or "deepseek-chat"
    base_url = base_url or os.environ.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com"
    if mode == "real":
        api_key = api_key or os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            result = {"results": [], "summary": {"total": 0}, "unfinished": True,
                      "reason": "真实模型模式未配置 API Key，未降级为 Mock"}
            tracer.finish(result, unfinished=True)
            return result, tracer
    fingerprint = hashlib.sha256()
    from pathlib import Path
    for source in sorted(Path(__file__).parent.glob("*.py")):
        fingerprint.update(source.name.encode())
        fingerprint.update(source.read_bytes())
    tracer.meta.update(mode=mode, model=model if mode == "real" else None,
                       base_url=base_url if mode == "real" else None,
                       started_at_utc=datetime.now(timezone.utc).isoformat(),
                       source_sha256=fingerprint.hexdigest())
    try:
        ctx = build_ctx(data_dir=data_dir, drafts_file=drafts_file, fail_save=fail_save, hard_targets=hard_targets(request))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        result = {"results": [], "summary": {"total": 0}, "unfinished": True,
                  "reason": f"加载业务数据失败（{type(exc).__name__}），任务未完成"}
        tracer.finish(result, unfinished=True)
        return result, tracer

    if mode == "real" and api_key:
        llm = OpenAICompatLLM(api_key=api_key, base_url=base_url, model=model)
    else:
        llm = MockLLM(request)

    result = run_agent(request, ctx, llm, max_steps=max_steps, tracer=tracer)
    return result, tracer

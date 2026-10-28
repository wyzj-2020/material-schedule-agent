"""Configuration and factual model trace regressions, without network calls."""
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from src.agent import AgentContext
from src.llm import OpenAICompatLLM, build_tool_schemas
from src.main import main
from src.pipeline import run
from src.protocol import Final, Observation
from src.tracer import TraceLogger


def test_cli_forwards_model_environment(monkeypatch, capsys):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-placeholder")
    monkeypatch.setenv("DEEPSEEK_MODEL", "configured-model")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://example.invalid/custom")
    with patch("src.main.load_env"), patch("src.main.run", return_value=({"results": []}, TraceLogger())) as runner:
        assert main(["--input", "examples/input_normal.json", "--real"]) == 0
    assert runner.call_args.kwargs["model"] == "configured-model"
    assert runner.call_args.kwargs["base_url"] == "https://example.invalid/custom"


def test_cli_explicit_model_overrides_environment(monkeypatch, capsys):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-placeholder")
    with patch("src.main.load_env"), patch("src.main.run", return_value=({"results": []}, TraceLogger())) as runner:
        main(["--input", "examples/input_normal.json", "--model", "explicit-model", "--base-url", "https://example.invalid"])
    assert runner.call_args.kwargs["model"] == "explicit-model"
    assert runner.call_args.kwargs["base_url"] == "https://example.invalid"


def test_explicit_real_does_not_fallback(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with patch("src.pipeline.load_env"):
        out, trace = run({"date": "2026-10-28", "materials": []}, mode="real")
    assert out["unfinished"]
    assert "Mock" in out["reason"]


@pytest.mark.parametrize("finish", [False, True])
def test_model_basis_is_preserved(finish):
    llm = OpenAICompatLLM("test-placeholder", "https://example.invalid", "configured-model")
    basis = "18:00 is occupied; inspect alternatives" if not finish else "Rule R1 blocks this material; stop scheduling"
    args = {"result": {"results": []}, "decision_basis": basis} if finish else {"material_id": "M01", "decision_basis": basis}
    response = Mock()
    response.json.return_value = {"choices": [{"message": {"tool_calls": [{"function": {
        "name": "finish" if finish else "read_material", "arguments": json.dumps(args)}}]}}]}
    with patch.object(llm.requests, "post", return_value=response):
        decision = llm.decide(AgentContext(request={}))
    assert decision.decision_basis == basis
    if finish:
        assert isinstance(decision, Final)
    else:
        assert "decision_basis" not in decision.args


def test_schemas_require_short_basis():
    assert all("decision_basis" in t["function"]["parameters"]["required"] for t in build_tool_schemas())


def test_provider_continuation_and_batched_tool_ids():
    llm = OpenAICompatLLM("test-placeholder", "https://example.invalid", "configured-model")
    calls = [{"id": f"provider-{i}", "type": "function", "function": {
        "name": "read_material", "arguments": json.dumps({
            "material_id": mid, "decision_basis": f"Read requested material {mid}"})}}
        for i, mid in enumerate(("M01", "M02"), start=1)]
    first, final = Mock(), Mock()
    first.json.return_value = {"choices": [{"message": {
        "content": None, "reasoning_content": "opaque-provider-continuation", "tool_calls": calls}}]}
    final.json.return_value = {"choices": [{"message": {"tool_calls": [{"id": "provider-finish", "function": {
        "name": "finish", "arguments": json.dumps({"result": {"results": []}, "decision_basis": "Inputs read; complete"})}}]}}]}
    ctx = AgentContext(request={}, steps=1)
    trace = TraceLogger()
    with patch.object(llm.requests, "post", side_effect=[first, final]) as post:
        for step in (1, 2):
            ctx.steps = step
            decision = llm.decide(ctx)
            obs = Observation(step, decision.tool, decision.args, {"found": True}, decision.decision_basis)
            ctx.observations.append(obs)
            trace.record(obs)
        ctx.steps = 3
        decision = llm.decide(ctx)
    assert post.call_count == 2
    messages = post.call_args.kwargs["json"]["messages"]
    assert messages[2]["reasoning_content"] == "opaque-provider-continuation"
    assert [m["tool_call_id"] for m in messages if m["role"] == "tool"] == ["provider-1", "provider-2"]
    trace.finish(decision.result, False, decision.decision_basis)
    assert "opaque-provider-continuation" not in json.dumps(trace.to_dict())


def test_empty_hard_conflict_fields_are_completed(tmp_path):
    from src.agent import run_agent
    from src.pipeline import build_ctx
    request = {"date": "2026-10-28", "materials": [{"material_id": "M05", "target_time": "18:00",
                                                        "requirements": [{"kind": "hard"}]}]}
    class MissingReasons:
        def decide(self, context):
            return Final({"results": [{"material_id": "M05", "review_status": "blocked",
                                      "suggested_slot": None, "risk_reasons": [], "pending_confirmations": []}]})
    ctx = build_ctx(drafts_file=str(tmp_path / "drafts.json"))
    out = run_agent(request, ctx, MissingReasons())
    assert out["results"][0]["risk_reasons"]
    assert out["results"][0]["pending_confirmations"]


@pytest.mark.parametrize("times", [("19:00", "21:00"), ("21:00", "19:00")])
def test_real_batch_validation_accepts_valid_orders_and_rejects_conflicts(times):
    from scripts.verify_real import verify_case
    out = {"unfinished": False, "results": [
        {"material_id": mid, "suggested_slot": {"date": "2026-10-28", "time": time},
         "review_status": "pending_confirmation", "pending_confirmations": ["Confirm changed time"],
         "draft_status": "saved"} for mid, time in zip(("M01", "M02"), times)]}
    assert verify_case("batch", out)
    out["results"][1]["suggested_slot"]["time"] = times[0]
    assert not verify_case("batch", out)
    out["results"][1]["suggested_slot"]["time"] = times[1]
    out["results"][0]["pending_confirmations"] = []
    assert not verify_case("batch", out)


def test_submission_archive_excludes_credentials_and_detects_leaks(tmp_path, monkeypatch):
    from scripts import build_submission
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / ".env").write_text("DEEPSEEK_API_KEY=fake-private-api-value-123456", encoding="utf-8")
    (root / ".env.example").write_text("DEEPSEEK_API_KEY=", encoding="utf-8")
    (root / "src/main.py").write_text("print('example')", encoding="utf-8")
    monkeypatch.setattr(build_submission, "ROOT", root)
    output = tmp_path / "safe.zip"
    build_submission.package(output)
    import zipfile
    with zipfile.ZipFile(output) as archive:
        assert "material-schedule-agent/.env" not in archive.namelist()
        assert "material-schedule-agent/.env.example" in archive.namelist()
    (root / "src/main.py").write_text("fake-private-api-value-123456", encoding="utf-8")
    with pytest.raises(ValueError, match="src"):
        build_submission.package(tmp_path / "unsafe.zip")
    assert not (tmp_path / "unsafe.zip").exists()

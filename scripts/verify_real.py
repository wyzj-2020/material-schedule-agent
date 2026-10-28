"""Generate and check fresh model-driven evidence using synthetic examples."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.pipeline import run


def verify_case(name, out):
    if out.get("unfinished"):
        return False
    by = {r["material_id"]: r for r in out["results"]}
    if name == "batch":
        # Models may preserve M02's requested 19:00 and put M01 at 21:00,
        # rather than following the Mock's iteration order. Both are valid.
        if set(by) != {"M01", "M02"}:
            return False
        slots = [r.get("suggested_slot") or {} for r in by.values()]
        if len({(s.get("date"), s.get("time")) for s in slots}) != 2:
            return False
        schedule = json.loads((Path(__file__).resolve().parents[1] / "data/existing_schedule.json").read_text(encoding="utf-8"))
        for mid, target in (("M01", "18:00"), ("M02", "19:00")):
            r = by[mid]
            slot = r.get("suggested_slot") or {}
            day = schedule.get(slot.get("date"), {})
            if slot.get("date") != "2026-10-28" or slot.get("time") not in day or day[slot["time"]]:
                return False
            if r.get("draft_status") not in ("saved", "duplicate"):
                return False
            if slot["time"] != target and (r.get("review_status") != "pending_confirmation" or not r.get("pending_confirmations")):
                return False
        return True
    if name == "normal":
        expected = {"M01": "19:00", "M02": "10:00"}
        return all(by.get(mid, {}).get("suggested_slot", {}).get("time") == time
                   and by[mid]["draft_status"] in ("saved", "duplicate")
                   for mid, time in expected.items()) and bool(by["M01"]["pending_confirmations"])
    if name == "normal_rerun":
        return len(by) == 2 and all(r["draft_status"] == "duplicate" for r in by.values())
    r = next(iter(by.values()), {})
    if name == "save_fail":
        return r.get("draft_status") == "failed" and bool(r.get("error"))
    expected = ("info_missing", "blocked") if name == "missing_info" else ("blocked",)
    return r.get("review_status") in expected and r.get("draft_status") == "none" and bool(r.get("risk_reasons"))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output-dir", required=True, help="A new directory for this run; historical evidence is preserved")
    p.add_argument("--check-existing", action="store_true", help="Recheck recorded outputs without calling a model")
    args = p.parse_args()
    output = Path(args.output_dir)
    if args.check_existing:
        path = output / "summary.json"
        summary = json.loads(path.read_text(encoding="utf-8"))
        initial = output / "summary.initial.json"
        if not initial.exists():
            initial.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        for item in summary["cases"]:
            folder = output / item["case"]
            out = json.loads((folder / "output.json").read_text(encoding="utf-8"))
            trace = json.loads((folder / "trace.json").read_text(encoding="utf-8"))
            item["passed"] = (verify_case(item["case"], out) and bool(trace["steps"])
                              and all(s.get("decision_basis") for s in trace["steps"])
                              and bool(trace["final"].get("decision_basis")))
        summary["passed"] = len(summary["cases"]) == 7 and all(x["passed"] for x in summary["cases"])
        summary["verification_note"] = "Batch validation checks no-conflict and confirmation invariants, allowing different valid model processing orders. Original check retained in summary.initial.json."
        path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=True))
        return 0 if summary["passed"] else 2
    output.mkdir(parents=True, exist_ok=False)
    project = Path(__file__).resolve().parents[1]
    cases = ["normal", "normal_rerun", "rule_risk", "missing_info", "hard_requirement", "save_fail", "batch"]
    summary = {"mode": "real", "cases": [], "passed": False}
    for name in cases:
        input_name = "normal" if name == "normal_rerun" else name
        request = json.loads((project / "examples" / f"input_{input_name}.json").read_text(encoding="utf-8"))
        folder = output / name
        folder.mkdir()
        (folder / "input.json").write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")
        drafts = output / ("normal" if name == "normal_rerun" else name) / "drafts.json"
        print(f"Running real model: {name}", flush=True)
        out, trace = run(request, mode="real", max_steps=24,
                         fail_save=name == "save_fail", drafts_file=str(drafts),
                         meta={"case": name, "input": f"examples/input_{input_name}.json"})
        (folder / "output.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        trace.write(str(folder / "trace.json"))
        try:
            passed = verify_case(name, out)
        except (KeyError, TypeError, AttributeError):
            passed = False
        bases = [s.get("decision_basis", "") for s in trace.steps]
        passed = passed and bool(bases) and all(bases) and bool((trace.final or {}).get("decision_basis"))
        item = {"case": name, "passed": passed, "meta": trace.meta,
                "steps": len(trace.steps), "unfinished": out.get("unfinished"), "reason": out.get("reason")}
        summary["cases"].append(item)
        summary["passed"] = len(summary["cases"]) == len(cases) and all(x["passed"] for x in summary["cases"])
        (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(item, ensure_ascii=True), flush=True)
        if not passed:
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Recovery and cross-process checks for the regression fixes."""
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from src.agent import run_agent
from src.llm import MockLLM
from src.pipeline import build_ctx, run
from src.protocol import ToolCall
from src.tools import SaveBackend
from src.tracer import TraceLogger

ROOT = Path(__file__).resolve().parents[1]
REQUEST = {'date': '2026-10-28', 'materials': [
    {'material_id': 'M01', 'target_time': '10:00', 'requirements': []}]}


def test_model_can_correct_invalid_tool(tmp_path):
    class Correcting:
        def decide(self, context):
            if not context.observations:
                return ToolCall('unknown_tool', {})
            assert context.observations[0].result['error']
            return delegate.decide(context)
    delegate = MockLLM(REQUEST)
    ctx = build_ctx(drafts_file=str(tmp_path / 'drafts.json'))
    out = run_agent(REQUEST, ctx, Correcting())
    assert not out['unfinished']
    assert out['results'][0]['draft_status'] == 'saved'


def test_model_failure_preserves_saved_work(tmp_path):
    class TimeoutAfterSave:
        def decide(self, context):
            if context.observations:
                raise TimeoutError('injected')
            return ToolCall('save_draft', {'material_id': 'M01', 'date': '2026-10-28',
                                         'time': '10:00', 'review_status': 'approved'})
    ctx = build_ctx(drafts_file=str(tmp_path / 'drafts.json'))
    trace = TraceLogger()
    out = run_agent(REQUEST, ctx, TimeoutAfterSave(), tracer=trace)
    assert out['unfinished'] and 'TimeoutError' in out['reason']
    assert out['results'][0]['draft_status'] == 'saved'
    assert trace.final['unfinished'] is True


def test_confirmation_rule_cannot_be_skipped_by_model(tmp_path):
    class SkipConfirmation:
        def decide(self, context):
            return ToolCall('save_draft', {'material_id': 'M01', 'date': '2026-10-28',
                                         'time': '10:00', 'review_status': 'approved'})
    ctx = build_ctx(drafts_file=str(tmp_path / 'drafts.json'))
    ctx.materials['M01'].duration_minutes = 60
    out = run_agent(REQUEST, ctx, SkipConfirmation(), max_steps=1)
    assert out['results'][0]['review_status'] == 'pending_confirmation'
    assert out['results'][0]['pending_confirmations']
    stored = json.loads((tmp_path / 'drafts.json').read_text(encoding='utf-8'))[0]
    assert stored['review_status'] == 'pending_confirmation'


def test_corrupt_store_returns_unfinished_without_overwriting(tmp_path):
    p = tmp_path / 'drafts.json'
    p.write_text('[{', encoding='utf-8')
    out, trace = run(REQUEST, drafts_file=str(p))
    assert out['unfinished'] and out['reason']
    assert p.read_text(encoding='utf-8') == '[{'
    assert trace.final['unfinished']


@pytest.mark.parametrize('same_slot', [False, True])
def test_process_writers_are_serialized(tmp_path, same_slot):
    # Each process loads before the shared go signal. Both instances are stale
    # by construction, rather than relying on a random thread scheduling race.
    code = '''
import json, sys, time
from pathlib import Path
from src.tools import SaveBackend
p, ready, go, mid, slot = sys.argv[1:]
b = SaveBackend(p)
Path(ready).write_text('ready')
deadline = time.monotonic() + 15
while not Path(go).exists():
    if time.monotonic() > deadline: raise RuntimeError('go timeout')
    time.sleep(.01)
print(json.dumps(b.save({'material_id':mid,'date':'2026-10-28','time':slot,'review_status':'approved'})))
'''
    go = tmp_path / 'go'
    ready = [tmp_path / 'ready1', tmp_path / 'ready2']
    p = tmp_path / 'drafts.json'
    children = []
    try:
        for i, slot in enumerate(['10:00', '10:00' if same_slot else '11:00']):
            children.append(subprocess.Popen(
                [sys.executable, '-c', code, str(p), str(ready[i]), str(go), f'M0{i+1}', slot],
                cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
        import time
        deadline = time.monotonic() + 15
        while not all(r.exists() for r in ready):
            assert time.monotonic() < deadline, 'worker startup timeout'
            time.sleep(.01)
        go.write_text('go')
        outcomes = []
        for child in children:
            stdout, stderr = child.communicate(timeout=20)
            assert child.returncode == 0, stderr
            outcomes.append(json.loads(stdout))
        assert sum(r['saved'] for r in outcomes) == (1 if same_slot else 2)
        records = SaveBackend(str(p)).list_drafts()
        assert len(records) == (1 if same_slot else 2)
        assert len({d['draft_id'] for d in records}) == len(records)
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.communicate()

"""Independent regression checks; run from the project root. No live API calls."""
import copy
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path.cwd()))
from src.agent import run_agent
from src.guardrails import hard_targets
from src.llm import MockLLM, OpenAICompatLLM
from src.pipeline import DATA_DIR, build_ctx, run
from src.protocol import Final, ToolCall
from src.tools import SaveBackend, save_draft
from src.tracer import TraceLogger


def request(ids=('M02',), time='10:00', hard=False):
    return {'date': '2026-10-28', 'materials': [
        {'material_id': mid, 'target_time': time, 'requirements': [
            {'kind': 'hard' if hard else 'soft', 'text': 'Must preserve date and time' if hard else time}
        ]} for mid in ids]}


def execute(tmp_path, q, **kwargs):
    (tmp_path / 'input.json').write_text(json.dumps(q, ensure_ascii=False, indent=2), encoding='utf-8')
    out, trace = run(q, mode='mock', drafts_file=str(tmp_path / 'drafts.json'), **kwargs)
    (tmp_path / 'output.json').write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8')
    trace.write(str(tmp_path / 'trace.json'))
    return out, trace


class Scripted:
    def __init__(self, actions):
        self.actions = iter(actions)

    def decide(self, context):
        return next(self.actions)


def scripted(tmp_path, q, actions, max_steps=12):
    with patch('src.pipeline.MockLLM', return_value=Scripted(actions)):
        return execute(tmp_path, q, max_steps=max_steps)


def result(mid='M02', date='2026-10-28', time='10:00', review='approved'):
    return {'results': [{'material_id': mid, 'review_status': review,
                        'suggested_slot': {'date': date, 'time': time},
                        'pending_confirmations': [], 'draft_status': 'saved', 'draft_id': 'FAKE'}]}


@pytest.mark.parametrize('name', ['normal', 'missing_info', 'rule_risk', 'hard_requirement', 'save_fail', 'batch'])
def test_cli_examples(tmp_path, name):
    cmd = [sys.executable, '-m', 'src.main', '--mock', '--input', f'examples/input_{name}.json',
           '--drafts-file', str(tmp_path / 'drafts.json'), '--output', str(tmp_path / 'output.json'),
           '--trace', str(tmp_path / 'trace.json')]
    if name == 'save_fail':
        cmd.append('--fail-save')
    proc = subprocess.run(cmd, capture_output=True, encoding='utf-8', timeout=20)
    assert proc.returncode == 0, proc.stderr
    out = json.loads((tmp_path / 'output.json').read_text(encoding='utf-8'))
    assert not out['unfinished']
    assert json.loads((tmp_path / 'trace.json').read_text(encoding='utf-8'))['final']['result'] == out
    if name == 'save_fail':
        assert out['results'][0]['draft_status'] == 'failed'


@pytest.mark.parametrize('hard', [False, True])
@pytest.mark.parametrize('target', ['10:00', '18:00', '19:00'])
def test_batch_and_repeat(tmp_path, hard, target):
    q = request(('M01', 'M02'), target, hard)
    first, _ = execute(tmp_path, q, max_steps=30)
    second, _ = execute(tmp_path, q, max_steps=30)
    drafts = json.loads((tmp_path / 'drafts.json').read_text(encoding='utf-8')) if (tmp_path / 'drafts.json').exists() else []
    keys = [(d['date'], d['time']) for d in drafts]
    assert len(keys) == len(set(keys))
    assert not first['unfinished'] and not second['unfinished']
    assert all(r['draft_status'] in ('duplicate', 'none') for r in second['results'])


@pytest.mark.parametrize('fail', [False, True])
@pytest.mark.parametrize('steps', [0, 1, 3, 4, 5, 6, 12])
def test_every_save_step_boundary(tmp_path, fail, steps):
    out, trace = execute(tmp_path, request(), fail_save=fail, max_steps=steps)
    saves = [s for s in trace.steps if s['tool'] == 'save_draft']
    assert len(trace.steps) <= steps
    if saves:
        assert len(out['results']) == 1
        assert out['results'][0]['draft_status'] == saves[-1]['result_summary']['draft_status']
        if fail:
            assert out['results'][0]['error']
    else:
        assert out['unfinished'] and not out['results']


@pytest.mark.parametrize('field,value', [('time', '19:00'), ('date', '2026-10-29'), ('date', None), ('time', None)])
@pytest.mark.parametrize('limit', [1, 12])
def test_hard_invalid_save_and_output(tmp_path, field, value, limit):
    q = request(('M05',), hard=True)
    draft = {'material_id': 'M05', 'date': '2026-10-28', 'time': '10:00', 'review_status': 'approved'}
    draft[field] = value
    out, _ = scripted(tmp_path, q, [ToolCall('save_draft', draft), Final(result('M05', draft['date'], draft['time']))], limit)
    assert not (tmp_path / 'drafts.json').exists()
    r = out['results'][0]
    assert r['draft_status'] == 'none'
    assert r['review_status'] in ('blocked', 'info_missing'), 'Rejected/incomplete schedule still reported approved'
    assert r['pending_confirmations'] or r['risk_reasons'], 'Rejection reason disappeared'


@pytest.mark.parametrize('duration', [59, 60, 61])
def test_rule_threshold(tmp_path, duration):
    ctx = build_ctx(drafts_file=str(tmp_path / 'drafts.json'))
    ctx.materials['M01'].duration_minutes = duration
    q = request(('M01',))
    trace = TraceLogger()
    out = run_agent(q, ctx, MockLLM(q), tracer=trace)
    trace.write(str(tmp_path / 'trace.json'))
    r = out['results'][0]
    assert r['review_status'] == ('approved' if duration < 60 else 'pending_confirmation')
    if duration >= 60:
        assert r['pending_confirmations']


def test_unknown_material(tmp_path):
    out, _ = execute(tmp_path, request(('M999',)))
    assert out['results'][0]['review_status'] in ('blocked', 'info_missing')
    assert out['results'][0]['draft_status'] == 'none'


def test_all_slots_occupied(tmp_path):
    ctx = build_ctx(drafts_file=str(tmp_path / 'drafts.json'))
    for t in ctx.schedule['2026-10-28']:
        ctx.schedule['2026-10-28'][t] = {'occupied_by': 'OTHER'}
    q = request()
    out = run_agent(q, ctx, MockLLM(q))
    assert out['results'][0]['review_status'] == 'blocked'
    assert not ctx.save_backend.list_drafts()


@pytest.mark.parametrize('stage', ['temp', 'partial', 'sync', 'replace'])
def test_atomic_failure(tmp_path, stage):
    p = tmp_path / 'drafts.json'
    b = SaveBackend(str(p))
    d = {'material_id': 'M02', 'date': '2026-10-28', 'time': '10:00', 'review_status': 'approved'}
    assert b.save(d)['saved']
    old = p.read_bytes()
    def partial(value, f, **kwargs):
        f.write('[{')
        f.flush()
        raise OSError('partial write fault')
    target = {'temp': 'src.tools.tempfile.mkstemp', 'partial': 'src.tools.json.dump',
              'sync': 'src.tools.os.fsync', 'replace': 'src.tools.os.replace'}[stage]
    kwargs = {'side_effect': partial if stage == 'partial' else OSError('injected failure')}
    with patch(target, **kwargs):
        r = b.save({**d, 'material_id': 'M01', 'time': '11:00'})
    assert r['draft_status'] == 'failed' and p.read_bytes() == old
    assert len(b.list_drafts()) == 1 and not list(tmp_path.glob('.drafts-*.tmp'))
    assert b.save({**d, 'material_id': 'M01', 'time': '11:00'})['draft_status'] == 'saved'
    assert len(SaveBackend(str(p)).list_drafts()) == 2


def test_paraphrase_dedup(tmp_path):
    b = SaveBackend(str(tmp_path / 'drafts.json'))
    d = {'material_id': 'M01', 'date': '2026-10-28', 'time': '19:00',
         'review_status': 'pending_confirmation', 'pending_confirmations': ['please confirm']}
    b.save(d)
    assert b.save({**d, 'pending_confirmations': ['confirmation required']})['draft_status'] == 'duplicate'
    assert len(b.list_drafts()) == 1


@pytest.mark.parametrize('mid', ['M03', 'M04'])
def test_model_ignores_missing_or_blocking_rule(tmp_path, mid):
    q = request((mid,))
    d = {'material_id': mid, 'date': '2026-10-28', 'time': '10:00', 'review_status': 'approved'}
    out, trace = scripted(tmp_path, q, [ToolCall('read_material', {'material_id': mid}),
        ToolCall('check_rule', {'material_id': mid}), ToolCall('save_draft', d), Final(result(mid))])
    assert not (tmp_path / 'drafts.json').exists(), 'Model saved material despite missing data or blocking rule'
    assert out['results'][0]['review_status'] != 'approved'


def test_model_saves_unrequested_material(tmp_path):
    d = {'material_id': 'M05', 'date': '2026-10-28', 'time': '10:00', 'review_status': 'approved'}
    scripted(tmp_path, request(), [ToolCall('save_draft', d), Final(result('M05'))])
    assert not (tmp_path / 'drafts.json').exists(), 'Unrequested material was saved'


def test_final_slot_matches_actual_saved_slot(tmp_path):
    d = {'material_id': 'M02', 'date': '2026-10-28', 'time': '10:00', 'review_status': 'approved'}
    out, _ = scripted(tmp_path, request(), [ToolCall('save_draft', d), Final(result(time='11:00'))])
    actual = json.loads((tmp_path / 'drafts.json').read_text(encoding='utf-8'))[0]
    assert out['results'][0]['suggested_slot']['time'] == actual['time'], 'Final claims saved at a different slot'


def test_pending_must_explain_confirmation(tmp_path):
    q = request(('M05',), time='18:00', hard=True)
    out, _ = scripted(tmp_path, q, [Final(result('M05', time='19:00', review='pending_confirmation'))])
    r = out['results'][0]
    assert r['review_status'] == 'blocked' or r['pending_confirmations'], 'Pending with no question or explanation'


@pytest.mark.parametrize('q', [
    {'date': '2026-10-28', 'materials': [{'material_id': 'M02'}]},
    {'date': '2026-10-28', 'materials': [{'target_time': '10:00'}]},
    {'date': '2026-10-28', 'materials': None},
])
def test_invalid_input_is_controlled(tmp_path, q):
    try:
        out, _ = execute(tmp_path, q)
    except ValueError:
        return  # A deliberate validation error is an acceptable API boundary.
    assert out.get('unfinished') or any(r['review_status'] in ('blocked', 'info_missing') for r in out['results'])


@pytest.mark.parametrize('date,time', [('not-a-date', '10:00'), ('2026-02-30', '10:00'), ('2026-10-28', '25:00')])
def test_invalid_date_time_not_saved(tmp_path, date, time):
    q = request(('M05',), time=time, hard=True)
    q['date'] = date
    d = {'material_id': 'M05', 'date': date, 'time': time, 'review_status': 'approved'}
    scripted(tmp_path, q, [ToolCall('save_draft', d), Final(result('M05', date, time))])
    assert not (tmp_path / 'drafts.json').exists(), 'Invalid calendar date or time saved'


@pytest.mark.parametrize('call', [ToolCall('unknown_tool', {}), ToolCall('read_material', {})])
def test_invalid_model_tool_is_controlled(tmp_path, call):
    out, trace = scripted(tmp_path, request(), [call, Final({'results': [], 'unfinished': True})])
    assert out['unfinished'] or trace.steps


def test_model_timeout_is_controlled(tmp_path):
    import requests
    llm = OpenAICompatLLM('fake-key', 'https://example.invalid', 'test-model')
    ctx = build_ctx(drafts_file=str(tmp_path / 'drafts.json'))
    with patch.object(llm.requests, 'post', side_effect=requests.exceptions.Timeout('injected timeout')):
        out = run_agent(request(), ctx, llm)
    assert out.get('unfinished') and out.get('reason')


def test_two_loaded_writers_do_not_lose_saved_data(tmp_path):
    p = str(tmp_path / 'drafts.json')
    first, second = SaveBackend(p), SaveBackend(p)
    d = {'material_id': 'M01', 'date': '2026-10-28', 'time': '10:00', 'review_status': 'approved'}
    assert first.save(d)['saved']
    assert second.save({**d, 'material_id': 'M02', 'time': '11:00'})['saved']
    assert len(SaveBackend(p).list_drafts()) == 2, 'Known concurrency limitation: stale writer overwrote saved draft'

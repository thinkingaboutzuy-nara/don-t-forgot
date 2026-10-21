#!/usr/bin/env python3
"""Check update_inbox.py with synthetic data in a temporary directory; never reads live task state."""
import argparse
import copy
import datetime as dt
import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
CANDIDATES = [HERE.parent / 'update_inbox.py', HERE.parent / 'assets/native-app/update_inbox.py']


def rejected(module, root, payload, label):
    before = (root / 'inbox.json').read_bytes()
    try:
        module.merge(root, payload)
    except ValueError:
        pass
    else:
        raise SystemExit(f'FAIL 应被拒绝：{label}')
    assert (root / 'inbox.json').read_bytes() == before, f'拒绝后清单被改动：{label}'


def verify(script):
    spec = importlib.util.spec_from_file_location('merge_under_test', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    task = {
        'id': 'fixture-1', 'title': '核对演示清单', 'detail': '合成验收数据。',
        'category': 'action', 'priority': 1, 'due': None,
        'sources': [{'chat': '演示', 'time': '2026-10-20 16:00', 'quote': '请核对。',
                     'messageId': 'fixture-message', 'conversationId': 'fixture-conversation'}],
    }
    payload = {'tasks': [task], 'sync': {'state': 'ok', 'mode': 'full', 'scope': '合成数据',
                                         'scannedThrough': '2026-10-20T16:00:00+08:00'}}
    with tempfile.TemporaryDirectory(prefix='todo-merge-check-') as tmp:
        root = pathlib.Path(tmp)
        module.merge(root, payload)
        actions = {'fixture-1': {'status': 'done', 'reason': '对方已处理', 'updatedAt': '2026-10-20T08:00:00Z'}}
        (root / 'actions.json').write_text(json.dumps(actions))
        original_actions = (root / 'actions.json').read_bytes()

        # Stable ID, no duplicates, manual actions untouched
        module.merge(root, payload)
        updated = copy.deepcopy(payload)
        updated['tasks'][0]['detail'] = '收到补充信息，仍是同一事项。'
        updated['sync']['mode'] = 'incremental'
        module.merge(root, updated)
        inbox = json.loads((root / 'inbox.json').read_text())
        assert len(inbox['tasks']) == 1
        assert inbox['sync']['lastFullScan'] == '2026-10-20T16:00:00+08:00'
        assert (root / 'actions.json').read_bytes() == original_actions

        # Failed coverage keeps the watermark
        module.merge(root, {'sync': {'state': 'error', 'message': '模拟分页失败', 'scannedThrough': '2026-10-21T16:00:00+08:00'}})
        inbox = json.loads((root / 'inbox.json').read_text())
        assert inbox['sync']['scannedThrough'] == '2026-10-20T16:00:00+08:00'

        # Invalid deltas are rejected atomically
        for field, value in [('category', 'unknown'), ('due', '2026-10-21T09:00:00'), ('waitingSince', 'yesterday'),
                             ('needsConfirm', 'yes')]:
            bad = copy.deepcopy(payload)
            bad['tasks'][0][field] = value
            rejected(module, root, bad, field)
        stale_no_reason = copy.deepcopy(payload)
        stale_no_reason['tasks'][0]['category'] = 'stale'
        rejected(module, root, stale_no_reason, 'stale 缺 resolution')
        dup = copy.deepcopy(payload)
        dup['tasks'][0]['id'] = 'fixture-2'
        rejected(module, root, dup, '同标题新 ID')
        rejected(module, root, {'sync': {'state': 'ok', 'scope': '合成', 'scannedThrough': '2026-10-19T16:00:00+08:00'}}, '水位倒退')
        rejected(module, root, {'sync': {'state': 'ok', 'scope': '合成', 'mode': 'weekly',
                                         'scannedThrough': '2026-10-21T16:00:00+08:00'}}, '无效 mode')

        # Stale needs a reason and stamps closedAt; reopening clears it
        stale = copy.deepcopy(payload['tasks'][0])
        stale.update(id='fixture-3', title='待确认：旧线索', category='stale', resolution='7 天无新进展，自动收起')
        module.merge(root, {'tasks': [stale]})
        row = next(t for t in json.loads((root / 'inbox.json').read_text())['tasks'] if t['id'] == 'fixture-3')
        assert row.get('closedAt'), '收起时应记录 closedAt'

        # Compact view: open tasks, feedback with reasons, hand-offs
        handoff = copy.deepcopy(payload['tasks'][0])
        handoff.update(id='fixture-4', title='交给 Codex 的事')
        module.merge(root, {'tasks': [handoff]})
        actions['fixture-4'] = {'handoffAt': '2026-10-21T02:00:00Z', 'updatedAt': '2026-10-21T02:00:00Z'}
        (root / 'actions.json').write_text(json.dumps(actions))
        view = module.open_view(root)
        assert [t['id'] for t in view['open']] == ['fixture-4'], view['open']
        assert view['feedback'][0]['reason'] == '对方已处理'
        assert view['handoffs'][0]['id'] == 'fixture-4'

        # Archive: closed more than 30 days moves out, title stays reserved, same ID restores
        inbox = json.loads((root / 'inbox.json').read_text())
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=40)).isoformat()
        for t in inbox['tasks']:
            if t['id'] == 'fixture-3':
                t['closedAt'] = old
        (root / 'inbox.json').write_text(json.dumps(inbox))
        result = module.merge(root, {'sync': {'state': 'ok', 'scope': '合成', 'scannedThrough': '2026-10-21T16:00:00+08:00'}})
        assert result['archived'] == 1, result
        inbox = json.loads((root / 'inbox.json').read_text())
        assert 'fixture-3' not in {t['id'] for t in inbox['tasks']}
        assert inbox['archived'][0]['id'] == 'fixture-3'
        assert any(r['id'] == 'fixture-3' for r in json.loads((root / inbox['archived'][0]['file']).read_text()))
        revive = copy.deepcopy(stale)
        revive['id'] = 'fixture-5'
        rejected(module, root, {'tasks': [revive]}, '已封存标题换 ID')
        back = copy.deepcopy(stale)
        back.update(category='action', detail='又有新进展')
        module.merge(root, {'tasks': [back]})
        inbox = json.loads((root / 'inbox.json').read_text())
        row = next(t for t in inbox['tasks'] if t['id'] == 'fixture-3')
        assert 'closedAt' not in row and not inbox['archived']
        assert (root / 'actions.json').read_bytes() == json.dumps(actions).encode()

    # Report platform link: pending ticks, platform-newer wins, validation
    with tempfile.TemporaryDirectory(prefix='todo-merge-report-') as tmp:
        root = pathlib.Path(tmp)
        linked = copy.deepcopy(task)
        linked.update(id='report-todo-t1', title='日报待办：核对验收', reportTodo={'id': 't1', 'version': 2, 'done': False})
        module.merge(root, {'tasks': [linked]})
        (root / 'actions.json').write_text(json.dumps({'report-todo-t1': {'status': 'done', 'reason': '办完了', 'updatedAt': '2026-10-21T08:00:00Z'}}))
        sync = module.open_view(root)['reportSync']
        assert sync == [{'id': 'report-todo-t1', 'todoId': 't1', 'expectedVersion': 2, 'done': True, 'at': '2026-10-21T08:00:00Z'}], sync
        # The platform was unticked later on the web: no push, and the task is open again.
        linked['reportTodo'] = {'id': 't1', 'version': 3, 'done': False, 'changedAt': '2026-10-21T09:00:00Z'}
        module.merge(root, {'tasks': [linked]})
        view = module.open_view(root)
        assert view['reportSync'] == [] and [t['id'] for t in view['open']] == ['report-todo-t1'], view
        bad = copy.deepcopy(linked)
        bad['reportTodo'] = {'id': 't1', 'version': '3', 'done': False}
        rejected(module, root, {'tasks': [bad]}, 'reportTodo version 类型')

    # Validation must not depend on assert statements
    with tempfile.TemporaryDirectory(prefix='todo-merge-O-') as tmp:
        bad = copy.deepcopy(payload)
        bad['tasks'][0]['category'] = 'unknown'
        delta = pathlib.Path(tmp) / 'delta.json'
        delta.write_text(json.dumps(bad))
        run = subprocess.run([sys.executable, '-O', str(script), '--data-dir', tmp, '--input', str(delta)],
                             capture_output=True, text=True)
        assert run.returncode != 0 and '增量被拒绝' in run.stderr, run
    print('PASS: stable ID, manual actions, failed coverage, atomic rejection, stale, open view, archive, report sync, python -O')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--script', type=pathlib.Path, default=next((p for p in CANDIDATES if p.exists()), CANDIDATES[0]))
    verify(parser.parse_args().script)

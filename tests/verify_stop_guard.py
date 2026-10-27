#!/usr/bin/env python3
"""Stop-hook contract tests; all ledgers and inboxes are synthetic."""
import copy
import datetime as dt
import json
import os
import pathlib
import subprocess
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import review_run as review
import update_inbox as inbox
import review_stop as guard


def fixture(project, session='fixture-owner', end=None):
    root = project / 'data'; runs = project / 'work/runs'
    config = {'timeZone': 'Asia/Taipei', 'profile': 'fixture:person', 'earliestDate': '2026-10-01',
              'dataDirectory': str(root), 'selfOpenDingTalkId': 'fixture-self', 'requireAutoCompletion': True}
    project.mkdir(parents=True, exist_ok=True)
    review.save(project / 'config.json', config)
    inbox.merge(root, {'tasks': []})
    (root / 'actions.json').write_text('{}')
    with patch.dict(os.environ, CODEX_THREAD_ID=session):
        key = review.start(runs, config, root, end=end)['run']
    return config, root, runs, key


def page(time):
    return {'success': True, 'result': {'hasMore': False, 'conversationMessagesList': [
        {'openConversationId': 'fixture-chat', 'title': '演示', 'singleChat': True,
         'messages': [{'openMessageId': 'fixture-message', 'sender': '演示同事',
                       'senderOpenDingTalkId': 'fixture-person', 'createTime': dt.datetime.fromisoformat(time).strftime('%Y-%m-%d %H:%M:%S'),
                       'content': '请核对演示事项。'}]}]}}


def complete(runs, key):
    state = review.load_run(runs, key)[1]
    review.ingest(runs, key, '0', {'success': True, 'result': {'hasMore': False, 'conversationMessagesList': []}})
    for check in state['requiredChecks']:
        review.check_stage(runs, key, check, '合成来源核对完成，无外部业务写入。')
    review.finish(runs, key, {'tasks': []}, '合成整理已完成。')


def verify():
    with tempfile.TemporaryDirectory(prefix='todo-stop-') as tmp:
        project = pathlib.Path(tmp) / 'project'
        config, root, runs, key = fixture(project)
        with patch.dict(os.environ, {}, clear=True):
            try:
                review.start(runs, config, root)
            except review.ReviewError:
                pass
            else:
                raise AssertionError('enabled completion guard silently started without a session identity')
        event = {'hook_event_name': 'Stop', 'session_id': 'fixture-owner', 'stop_hook_active': False}
        before = (root / 'actions.json').read_bytes()
        assert guard.handle(project, {**event, 'session_id': 'unrelated-chat'}) == {}
        first = guard.handle(project, event)
        assert first['decision'] == 'block' and 'fetch-command' in first['reason']
        review.ingest(runs, key, '0', page(review.load_run(runs, key)[1]['end']))
        review.record_failure(runs, key, '191条待审阅，下一轮继续，尚未完成。')
        second = guard.handle(project, {**event, 'stop_hook_active': True})
        assert second['decision'] == 'block' and ' next ' in second['reason'], 'progress-only fail must continue'
        batch = review.next_chunks(runs, key)['chunks']
        review.acknowledge(runs, key, [{'id': c['id'], 'sha': c['sha'], 'note': '完整阅读演示承诺，下一步核对。'} for c in batch])
        assert guard.handle(project, event)['decision'] == 'block', 'actual progress resets stalled counter'
        assert guard.handle(project, event)['decision'] == 'block'
        stalled = guard.handle(project, event)
        assert '无进展' in stalled['systemMessage'] and 'decision' not in stalled
        assert review.read(root / 'inbox.json')['sync']['state'] == 'partial'
        assert (root / 'actions.json').read_bytes() == before

    with tempfile.TemporaryDirectory(prefix='todo-stop-failure-') as tmp:
        project = pathlib.Path(tmp) / 'project'
        config, root, runs, key = fixture(project)
        event = {'hook_event_name': 'Stop', 'session_id': 'fixture-owner'}
        evidence = runs / key / 'source-error.json'
        review.save(evidence, {'kind': 'authentication', 'operation': 'dws fixture-read',
                               'status': 'error', 'error': 'fixture login expired'})
        review.record_failure(runs, key, '合成登录已过期，需要本人重新登录。', evidence=evidence,
                              expected_session='fixture-owner', expected_fingerprint=review.work_fingerprint(review.load_run(runs, key)[1]))
        assert 'decision' not in guard.handle(project, event), 'genuine source failure may stop'
        review.save(evidence, {'status': 'ok'})
        assert guard.handle(project, event)['decision'] == 'block', 'changed evidence must not authorize stopping'

    with tempfile.TemporaryDirectory(prefix='todo-stop-owner-race-') as tmp:
        project = pathlib.Path(tmp) / 'project'
        config, root, runs, key = fixture(project)
        event = {'hook_event_name': 'Stop', 'session_id': 'fixture-owner'}
        guard.handle(project, event); guard.handle(project, event)
        snapshot = (root / 'inbox.json').read_bytes()
        real_failure = review.record_failure
        def take_over_before_failure(*args, **kwargs):
            review.start(runs, config, root, session_id='new-owner')
            return real_failure(*args, **kwargs)
        with patch.object(review, 'record_failure', take_over_before_failure):
            try:
                guard.handle(project, event)
            except review.ReviewError:
                pass
        assert review.load_run(runs, key)[1].get('lastError') is None, 'old hook wrote failure after ownership transfer'
        assert (root / 'inbox.json').read_bytes() == snapshot

    with tempfile.TemporaryDirectory(prefix='todo-stop-finish-') as tmp:
        project = pathlib.Path(tmp) / 'project'
        config, root, runs, key = fixture(project)
        complete(runs, key)
        snapshot = (root / 'inbox.json').read_bytes()
        assert guard.handle(project, {'hook_event_name': 'Stop', 'session_id': 'fixture-owner'}) == {}
        assert (root / 'inbox.json').read_bytes() == snapshot

    with tempfile.TemporaryDirectory(prefix='todo-stop-catchup-') as tmp:
        project = pathlib.Path(tmp) / 'project'
        old_end = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).isoformat()
        config, root, runs, key = fixture(project, end=old_end)
        complete(runs, key)
        event = {'hook_event_name': 'Stop', 'session_id': 'fixture-owner'}
        result = guard.handle(project, event)
        assert result['decision'] == 'block'
        followup = review.read(runs / '.active.json')['run']
        assert followup != key
        fixed_end = review.load_run(runs, followup)[1]['end']
        guard.handle(project, event)
        assert review.load_run(runs, followup)[1]['end'] == fixed_end
        complete(runs, followup)
        assert guard.handle(project, event) == {}, 'catch up once, never chase arriving messages forever'
        payload = json.dumps(event).encode()
        command = [sys.executable, str(pathlib.Path(guard.__file__)), '--project', str(project)]
        p = subprocess.run(command, input=payload, capture_output=True)
        assert p.returncode == 0 and json.loads(p.stdout) == {}
        # A different session starting work must not be stolen during catch-up.
        with patch.dict(os.environ, CODEX_THREAD_ID='other-owner'):
            other = review.start(runs, config, root)['run']
        try:
            review.start(runs, config, root, session_id='fixture-owner', expected_complete_run=followup)
        except review.ReviewError:
            pass
        else:
            raise AssertionError('concurrent continuation stole another run')
        assert review.load_run(runs, other)[1]['completionSessionId'] == 'other-owner'

    with tempfile.TemporaryDirectory(prefix='todo-stop-identity-') as tmp:
        project = pathlib.Path(tmp) / 'project'
        old_end = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).isoformat()
        config, root, runs, key = fixture(project, end=old_end)
        complete(runs, key)
        review.save(project / 'config.json', {**config, 'profile': 'other-account', 'dataDirectory': str(project / 'other-data')})
        try:
            guard.handle(project, {'hook_event_name': 'Stop', 'session_id': 'fixture-owner'})
        except review.ReviewError:
            pass
        else:
            raise AssertionError('automatic catch-up crossed configured identity')
        assert review.read(runs / '.active.json')['run'] == key
        assert not (project / 'other-data').exists()
    print('PASS: automatic continuation, owner isolation, progress-only failure, bounded stalls, real blocker, completion and fixed catch-up')


if __name__ == '__main__':
    verify()

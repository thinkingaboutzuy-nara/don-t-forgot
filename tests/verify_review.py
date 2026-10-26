#!/usr/bin/env python3
"""Synthetic interrupted/resumed review, coverage gate and crash recovery checks."""
import copy
import json
import os
import pathlib
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import review_run as review
import update_inbox as inbox


def verify_utf8_cli():
    """Exercise the saved-JSON workflow with a non-UTF-8 scheduler locale."""
    with tempfile.TemporaryDirectory(prefix='todo-encoding-') as tmp:
        project = pathlib.Path(tmp); root = project / 'data'
        config = {'timeZone': 'Asia/Taipei', 'profile': 'fixture:person', 'earliestDate': '2026-10-01',
                  'dataDirectory': str(root), 'verifyMessagePages': True}
        (project / 'config.json').write_text(json.dumps(config, ensure_ascii=False), encoding='utf-8')
        env = dict(os.environ, LC_ALL='C', LANG='C', PYTHONUTF8='0', PYTHONCOERCECLOCALE='0', PYTHONIOENCODING='ascii')
        def cli(*args, payload=None, ok=True):
            p = subprocess.run([sys.executable, str(pathlib.Path(review.__file__)), '--project', str(project), *args],
                               input=json.dumps(payload, ensure_ascii=False).encode('utf-8') if payload is not None else None,
                               capture_output=True, env=env)
            assert (p.returncode == 0) == ok, (args, p.stderr.decode('utf-8', errors='replace'))
            return json.loads(p.stdout.decode('utf-8')) if ok else p.stderr.decode('utf-8')
        run = cli('start', '--end', '2026-10-20T09:00:00+08:00')['run']
        msg = page('', False, '核对中文候选：引号“测试”、换行\n及表情🙂。')
        cli('ingest', '--run', run, '--cursor', '0', payload=msg)
        assert cli('ingest', '--run', run, '--cursor', '0', payload=msg)['phase'] == 'review'
        for chunk in cli('next', '--run', run)['chunks']:
            path = project / 'ack.json'
            path.write_text(json.dumps([{'id': chunk['id'], 'sha': chunk['sha'], 'note': '已阅读中文消息，保留引号、换行及表情。'}], ensure_ascii=False), encoding='utf-8')
            cli('ack', '--run', run, '--input', str(path))
        assert cli('start', '--end', '2026-10-20T10:00:00+08:00')['run'] == run
        for key in review.STAGES:
            cli('check', '--run', run, '--key', key, '--note', '合成核对已完成，不执行任何外部操作。')
        task = {'id': 'fixture-utf8', 'title': '确认中文待办🙂', 'detail': '含“引号”和换行\n仍需保留',
                'category': 'action', 'priority': 1, 'sources': [{'chat': '演示', 'time': '2026-10-20 08:00:00',
                'quote': '核对中文候选', 'messageId': 'fixture-msg', 'conversationId': 'fixture-chat'}]}
        delta = project / 'delta.json'
        delta.write_text(json.dumps({'tasks': [task]}, ensure_ascii=False), encoding='utf-8')
        bad = project / 'bad.json'; bad.write_bytes(b'{"tasks": []}\xef')
        cli('finish', '--run', run, '--input', str(bad), '--message', '不得合并损坏的候选', ok=False)
        assert not (root / 'inbox.json').exists()
        result = cli('finish', '--run', run, '--input', str(delta), '--message', '中文候选已合并并读回🙂')
        saved = json.loads((root / 'inbox.json').read_text(encoding='utf-8'))
        assert saved['tasks'][0]['title'] == task['title'] and saved['tasks'][0]['detail'] == task['detail']
        assert result['sync']['scannedThrough'] == '2026-10-20T09:00:00+08:00'
        p = subprocess.run([sys.executable, str(pathlib.Path(inbox.__file__)), '--data-dir', str(root), '--open'], capture_output=True, env=env)
        assert p.returncode == 0, p.stderr.decode('utf-8', errors='replace')
        assert json.loads(p.stdout.decode('utf-8'))['open'][0]['title'] == task['title']
    print('PASS: saved UTF-8 JSON review CLI, non-UTF-8 locale, interruption resume, same-ID merge/readback')


def rejects(call, label):
    try:
        call()
    except ValueError:
        return
    raise AssertionError('Should reject: ' + label)


def page(cursor='next', more=True, content='请核对演示事项。'):
    return {'success': True, 'result': {'hasMore': more, 'nextCursor': cursor,
            'conversationMessagesList': [{'openConversationId': 'fixture-chat', 'title': '演示', 'singleChat': True,
                'messages': [{'openConversationId': 'fixture-chat', 'openMessageId': 'fixture-msg',
                              'createTime': '2026-10-20 08:00:00', 'sender': '演示同事',
                              'senderOpenDingTalkId': 'fixture-person', 'content': content}]}]}}


def verify_page_consistency():
    with tempfile.TemporaryDirectory(prefix='todo-page-proof-') as tmp:
        tmp = pathlib.Path(tmp); root = tmp / 'data'; runs = tmp / 'runs'
        config = {'timeZone': 'Asia/Taipei', 'profile': 'fixture:person', 'earliestDate': '2026-10-01', 'verifyMessagePages': True}
        run = review.start(runs, config, root, '2026-10-20T09:00:00+08:00')['run']
        partial = page(); full = copy.deepcopy(partial)
        extra = copy.deepcopy(full['result']['conversationMessagesList'][0]['messages'][0]); extra['openMessageId'] = 'fixture-second'; extra['content'] = '需要跟进的第二件演示事项'
        full['result']['conversationMessagesList'][0]['messages'].append(extra)
        assert review.ingest(runs, run, '0', partial)['pages'] == 0
        assert review.ingest(runs, run, '0', full)['pages'] == 0
        assert review.ingest(runs, run, '0', full)['pages'] == 1
        empty_final = {'success': True, 'result': {'hasMore': False, 'conversationMessagesList': []}}
        assert review.ingest(runs, run, 'next', empty_final)['phase'] == 'fetch'
        assert review.ingest(runs, run, 'next', empty_final)['phase'] == 'review'
        assert review.load_run(runs, run)[1]['messageCount'] == 2
        while True:
            batch = review.next_chunks(runs, run, 3)['chunks']
            if not batch: break
            review.acknowledge(runs, run, [{'id': c['id'], 'sha': c['sha'], 'note': '两条合成业务消息均已完整核对。'} for c in batch])
        for k in review.load_run(runs, run)[1]['requiredChecks']: review.check_stage(runs, run, k, '本项为合成数据核对，通过，不执行外部写入。')
        review.finish(runs, run, {'tasks': []}, '合成分页复核通过')
        witness = review.read(runs / run / 'message-ids.json')
        assert len(witness) == 2 and all('content' not in m for m in witness)
        run2 = review.start(runs, config, root, '2026-10-20T10:00:00+08:00')['run']
        empty_more = {'success': True, 'result': {'hasMore': True, 'nextCursor': 'later', 'conversationMessagesList': []}}
        review.ingest(runs, run2, '0', empty_more); review.ingest(runs, run2, '0', empty_more)
        rejects(lambda: review.ingest(runs, run2, '0', empty_more), 'empty continuation must never be accepted')
        state = review.load_run(runs, run2)[1]
        assert state['cursor'] == '0' and not state['pages'] and state['pendingPage']['attempts'] == 3
        assert review.read(root / 'inbox.json')['sync']['scannedThrough'] == '2026-10-20T09:00:00+08:00'
    print('PASS: partial-page recovery, repeated empty continuation rejection, final-page verification, identity witness')


def verify():
    with tempfile.TemporaryDirectory(prefix='todo-review-') as tmp:
        tmp = pathlib.Path(tmp); root = tmp / 'data'; runs = tmp / 'runs'
        task = {'id': 'fixture-task', 'title': '核对演示事项', 'detail': '合成数据', 'category': 'action',
                'priority': 1, 'sources': [{'chat': '演示', 'time': '2026-10-20 08:00:00', 'quote': '请核对',
                                           'messageId': 'fixture-msg', 'conversationId': 'fixture-chat'}]}
        inbox.merge(root, {'tasks': [task], 'sync': {'state': 'ok', 'scope': '合成', 'mode': 'full',
                    'scannedThrough': '2026-10-20T08:00:00+08:00'}})
        # Preserve arbitrary manual state bytes, including future fields.
        (root / 'actions.json').write_text('{"fixture-task":{"status":"snoozed","until":9999999999,"updatedAt":"2026-10-20T00:00:00Z","reason":"稍后处理"}}')
        manual = (root / 'actions.json').read_bytes()
        config = {'timeZone': 'Asia/Taipei', 'profile': 'fixture:person', 'earliestDate': '2026-10-01'}
        run = review.start(runs, config, root, '2026-10-20T09:00:00+08:00', True)['run']
        path, state = review.load_run(runs, run)
        baseline = (root / 'inbox.json').read_bytes()
        rejects(lambda: inbox.merge(root, {'sync': {'state': 'ok', 'scope': '未读也声称成功',
                'scannedThrough': '2026-10-20T09:00:00+08:00'}}, review_root=runs), 'missing receipt')
        rejects(lambda: review.finish(runs, run, {}, '还没抓取'), 'fetch incomplete')
        rejects(lambda: review.ingest(runs, run, '0', {'success': False}), 'API failure is not empty')
        rejects(lambda: review.ingest(runs, run, '0', page('0')), 'stalled cursor')
        outside = page(); outside['result']['conversationMessagesList'][0]['messages'][0]['createTime'] = '2026-09-30 00:00:00'
        rejects(lambda: review.ingest(runs, run, '0', outside), 'outside boundary')
        assert (root / 'inbox.json').read_bytes() == baseline
        text = '合成的完整文本。' * 2500
        first = page(content=text)
        review.ingest(runs, run, '0', first)
        # Retry same page is idempotent; alternate payload or skipping a cursor is rejected.
        review.ingest(runs, run, '0', first)
        rejects(lambda: review.ingest(runs, run, '0', page(content='另一个内容')), 'same cursor changed')
        rejects(lambda: review.ingest(runs, run, 'wrong', page()), 'skip page')
        resumed = review.start(runs, config, root, '2026-10-21T10:00:00+08:00', True)
        assert resumed['run'] == run and resumed['cursor'] == 'next' and resumed['pages'] == 1
        assert resumed['end'] == '2026-10-20T09:00:00+08:00', 'Resume must not move the end'
        rejects(lambda: review.start(runs, {**config, 'profile': 'other'}, root), 'identity drift')
        # Empty page with hasMore=true still advances; only false finishes the fetch.
        empty = {'success': True, 'result': {'hasMore': True, 'nextCursor': 'last', 'conversationMessagesList': []}}
        review.ingest(runs, run, 'next', empty)
        review.ingest(runs, run, 'last', {'success': True, 'result': {'hasMore': False, 'conversationMessagesList': []}})
        state = review.load_run(runs, run)[1]
        assert state['messageCount'] == 1 and len(state['chunks']) >= 3
        chunks = review.read(path / 'chunks.json')
        assert all(len(c['text']) <= review.CHUNK_CHARS for c in chunks)
        assert text in ''.join(c['text'] for c in chunks), 'Long messages must not be truncated'
        rejects(lambda: review.acknowledge(runs, run, [{'id': chunks[0]['id'], 'sha': chunks[0]['sha'], 'note': '尚未显示不能确认'}]), 'unseen chunk')
        rejects(lambda: review.acknowledge(runs, run, [{'id': 'fake', 'note': '空哈希不能绕过检查'}]), 'null hash bypass')
        rejects(lambda: inbox.merge(root, {'sync': {'scannedThrough': '2026-10-20T09:00:00+08:00'}}, review_root=runs), 'implicit success bypass')
        batch = review.next_chunks(runs, run, 1)['chunks']
        review.acknowledge(runs, run, [{'id': c['id'], 'sha': c['sha'], 'note': '合成文本已核对，无新增业务事项。'} for c in batch])
        review.record_failure(runs, run, '合成网络故障，服务恢复后继续剩余审阅。')
        assert (path / 'pages').exists() and (path / 'chunks.json').exists()
        assert review.read(root / 'inbox.json')['sync']['scannedThrough'] == '2026-10-20T08:00:00+08:00'
        resumed = review.start(runs, config, root)
        assert resumed['reviewed'] == 1
        rejects(lambda: review.finish(runs, run, {}, '未审阅完'), 'review incomplete')
        while True:
            batch = review.next_chunks(runs, run, 3)['chunks']
            if not batch: break
            review.acknowledge(runs, run, [{'id': c['id'], 'sha': c['sha'], 'note': '已完整阅读合成文本，未发现其他事项。'} for c in batch])
        rejects(lambda: review.finish(runs, run, {}, '尚缺核对'), 'required checks missing')
        for key in state['requiredChecks']:
            review.check_stage(runs, run, key, '合成来源与同ID状态核对通过，无外部写入。')
        desired = {**task, 'detail': '已按来源核对，继续跟进。'}
        payload = {'tasks': [desired]}
        # A finish that completed the inbox write but crashed before marking the ledger can recover.
        real_merge = inbox.merge
        def merge_then_crash(*args, **kwargs):
            real_merge(*args, **kwargs)
            raise RuntimeError('synthetic crash after commit')
        inbox.merge = merge_then_crash
        try:
            try: review.finish(runs, run, payload, '合成整理完成')
            except RuntimeError: pass
            else: raise AssertionError('Fault did not occur')
        finally:
            inbox.merge = real_merge
        assert review.load_run(runs, run)[1]['phase'] == 'review'
        result = review.finish(runs, run, payload, '合成整理完成')
        assert result['recovered'] and review.load_run(runs, run)[1]['phase'] == 'complete'
        assert not (path / 'pages').exists() and not (path / 'messages.json').exists()
        assert review.read(root / 'inbox.json')['sync']['scannedThrough'] == '2026-10-20T09:00:00+08:00'
        assert (root / 'actions.json').read_bytes() == manual
        rejects(lambda: review.finish(runs, run, {'tasks': []}, '另一份增量'), 'replay altered payload')
        followup = review.start(runs, config, root, '2026-10-20T10:00:00+08:00')
        assert followup['run'] != run and followup['mode'] == 'incremental'
        assert followup['start'] == '2026-10-20T08:45:00+08:00'
        review.prune_completed(runs, keep=0)
        assert (runs / followup['run'] / 'review.json').exists(), 'Cleanup must preserve unfinished work'
        assert not path.exists()
        assert review.safe_text('密码: Example123!') .startswith('[登录材料')
        assert review.safe_text('examplelogin1234').startswith('[登录材料')
        assert review.safe_text('123456').startswith('[登录材料')
        assert review.safe_text('密码在后面 Example123!').startswith('[登录材料')
        assert review.safe_text('https://Example.test/a?x=123Ab!') == '[链接]'
        assert 'signature' not in review.safe_text('详情 https://example.test/a?signature=secret')
    print('PASS: resumable pages/read progress, bounded lossless chunks, identity/cursor guards, completion gate, manual state, crash recovery, cleanup')


if __name__ == '__main__':
    verify_utf8_cli()
    verify()
    verify_page_consistency()

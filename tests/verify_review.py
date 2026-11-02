#!/usr/bin/env python3
"""Synthetic interrupted/resumed review, coverage gate and crash recovery checks."""
import copy
import json
import os
import pathlib
import subprocess
import shlex
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import review_run as review
import update_inbox as inbox


def verify_fixed_candidate():
    with tempfile.TemporaryDirectory(prefix='todo-candidate-') as tmp:
        project = pathlib.Path(tmp); root = project / 'data'; runs = project / 'work/runs'
        config = {'timeZone': 'Asia/Taipei', 'profile': "fixture:person;$(exit 9)", 'earliestDate': '2026-10-01',
                  'dataDirectory': str(root), 'selfOpenDingTalkId': 'fixture-self', 'verifyMessagePages': True}
        old = {'id': 'fixture-old', 'title': '核对演示事项', 'detail': '保留原背景', 'category': 'waiting', 'priority': 1,
               'sources': [{'chat': '旧会话', 'time': '2026-10-20 07:00:00', 'quote': '原依据', 'messageId': 'old-msg', 'conversationId': 'old-chat'}]}
        inbox.merge(root, {'tasks': [old]})
        (root / 'actions.json').write_text('{"fixture-old":{"status":"dismissed","reason":"已在别处跟进"}}', encoding='utf-8')
        manual = (root / 'actions.json').read_bytes(); before = (root / 'inbox.json').read_bytes()
        run = review.start(runs, config, root, '2026-10-20T09:00:00+08:00')['run']
        request = review.fetch_command(runs, run, config, project)
        assert shlex.split(request['command'].splitlines()[1].split(' | ')[0])[-3:] == [config['profile'], '--format', 'json']
        assert (root / 'inbox.json').read_bytes() == before
        msg = page('', False)
        outgoing = copy.deepcopy(msg['result']['conversationMessagesList'][0]['messages'][0])
        outgoing.update(openMessageId='fixture-msg2', senderOpenDingTalkId='fixture-self', sender='本人')
        msg['result']['conversationMessagesList'][0]['messages'].append(outgoing)
        msg['result']['conversationMessagesList'].append({'openConversationId': 'group-chat', 'title': '演示群', 'singleChat': False,
            'messages': [{**outgoing, 'openConversationId': 'group-chat', 'openMessageId': 'fixture-msg3'}]})
        review.ingest(runs, run, '0', msg); review.ingest(runs, run, '0', msg)
        doc = {'summary': '合成候选已核对', 'tasks': [{'id': 'fixture-old', 'appendDetail': '补充新进度', 'messageIndexes': [1, 1]},
                    {'id': 'fixture-new', 'fields': {'title': '核对群内新要求', 'detail': '已阅读新的独立要求', 'category': 'action', 'priority': 1}, 'messageIndexes': [2, 3]}],
               'checks': {k: '合成核对已完成，无外部系统操作。' for k in review.load_run(runs, run)[1]['requiredChecks']}}
        rejects(lambda: review.stage_candidate(runs, run, doc, config), 'unread messages')
        chunks = review.next_chunks(runs, run, 3)['chunks']
        review.acknowledge(runs, run, [{'id': chunk['id'], 'sha': chunk['sha'], 'note': '已完整阅读演示新进度，按原ID更新。'} for chunk in chunks])
        original_state = (runs / run / 'review.json').read_bytes()
        bad = copy.deepcopy(doc); bad['tasks'][0]['messageIndexes'] = [99]
        rejects(lambda: review.stage_candidate(runs, run, bad, config), 'unknown source index')
        assert (runs / run / 'review.json').read_bytes() == original_state
        assert not (runs / run / 'delta.json').exists()
        bad = copy.deepcopy(doc); bad['checks']['task:wrong'] = '不可登记不存在的核对事项。'
        rejects(lambda: review.stage_candidate(runs, run, bad, config), 'unknown check')
        assert (runs / run / 'review.json').read_bytes() == original_state
        # Falsy wrong types used to bypass validation and break Swift Inbox decoding.
        for fields in [{'due': False}, {'waitingSince': False}, {'priority': True}, {'resolution': True},
                       {'dispatch': {'threadId': False}}, {'detail': []},
                       {'reportTodo': {'id': 'fixture-report', 'version': 1, 'done': False, 'reportId': False}}]:
            bad = copy.deepcopy(doc); bad['tasks'][0]['fields'] = fields
            if 'detail' in fields: bad['tasks'][0].pop('appendDetail')
            rejects(lambda: review.stage_candidate(runs, run, bad, config), 'typed task fields')
            assert (runs / run / 'review.json').read_bytes() == original_state
            assert not (runs / run / 'delta.json').exists()
        bad = copy.deepcopy(doc)
        bad['tasks'][0]['sources'] = [{**old['sources'][0], 'messageId': 'bad-direction', 'recipientName': False}]
        rejects(lambda: review.stage_candidate(runs, run, bad, config), 'typed source direction')
        assert (root / 'inbox.json').read_bytes() == before
        review.stage_candidate(runs, run, doc, config)
        candidate = review.read(runs / run / 'delta.json')
        t = candidate['tasks'][0]
        assert t['title'] == old['title'] and t['detail'] == '保留原背景\n补充新进度'
        assert len(t['sources']) == 2 and t['sources'][0] == old['sources'][0]
        assert t['sources'][1]['messageId'] == 'fixture-msg' and t['sources'][1]['recipientName'] == '我'
        assert candidate['tasks'][1]['sources'][0]['recipientName'] == '演示'
        assert candidate['tasks'][1]['sources'][1]['recipientName'] == '演示群'
        assert candidate['tasks'][1]['sources'][1]['conversationType'] == 'group'
        assert (root / 'inbox.json').read_bytes() == before, 'stage must not publish'
        resumed = review.start(runs, config, root, '2026-10-20T10:00:00+08:00')
        assert resumed['run'] == run and not resumed['remainingChecks']
        edited = copy.deepcopy(candidate); edited['tasks'][0]['detail'] = '绕过候选修改'
        rejects(lambda: review.finish(runs, run, edited, doc['summary']), 'changed staged candidate')
        review.finish(runs, run, candidate, doc['summary'])
        assert (root / 'actions.json').read_bytes() == manual
        assert [t['id'] for t in inbox.open_view(root)['open']] == ['fixture-new'], 'manual dismissal remains effective'
        assert review.read(root / 'inbox.json')['tasks'][0]['sources'][1]['senderId'] == 'fixture-person'
    print('PASS: fixed JSON candidate, source mapping/dedup, input rejection, resume, manual state, staged hash')


def verify_utf8_cli():
    """Exercise the saved-JSON workflow with a non-UTF-8 scheduler locale."""
    with tempfile.TemporaryDirectory(prefix='todo-encoding-') as tmp:
        project = pathlib.Path(tmp); root = project / 'data'
        config = {'timeZone': 'Asia/Taipei', 'profile': 'fixture:person', 'earliestDate': '2026-10-01',
                  'dataDirectory': str(root), 'selfOpenDingTalkId': 'fixture-self', 'verifyMessagePages': True}
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
        document = {'summary': '中文候选已合并并读回🙂', 'tasks': [{'id': task['id'],
                    'fields': {k: v for k, v in task.items() if k not in ['id', 'sources']}, 'messageIndexes': [1]}],
                    'checks': {k: '合成核对已完成，不执行任何外部操作。' for k in review.STAGES}}
        decisions = project / 'decisions.json'
        decisions.write_text(json.dumps(document, ensure_ascii=False), encoding='utf-8')
        cli('stage', '--run', run, '--input', str(decisions))
        bad = project / 'bad.json'; bad.write_bytes(b'{"tasks": []}\xef')
        cli('finish', '--run', run, '--input', str(bad), '--message', '不得合并损坏的候选', ok=False)
        assert not (root / 'inbox.json').exists()
        result = cli('finish', '--run', run)
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
        assert review.read(root / 'inbox.json')['sync']['failedRunId'] == run, 'the failure points at the failing run'
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
        assert review.read(root / 'inbox.json')['sync']['failedRunId'] is None, 'success clears the failure pointer'
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


def verify_api_redaction():
    # Synthetic values cover the leaked shapes without copying real credentials.
    long_key = 'sk-' + 'Fixture9Only_' * 12
    bare_key = 'Fixture9Only' * 11
    dotted_key = 'FixturePrefix9OnlyABC.' + bare_key
    samples = [long_key, 'https://example.test/provider\n\n' + bare_key,
               '请配置 API_KEY="' + bare_key + '"，然后核对服务。',
               '测试值 ' + long_key + ' 配好后再确认。']
    for text in samples:
        masked = review.safe_text(text)
        assert long_key not in masked and bare_key not in masked, 'API credential body was not redacted'
        assert review.safe_text(masked) == masked, 'redaction must be idempotent'
    assert '然后核对服务' in review.safe_text(samples[2]), 'business context must survive API-key removal'
    business = '请在2026-11-02核对接口与订单20261015-003，保留消息依据。'
    assert review.safe_text(business) == business
    assert 'FixturePrefix' not in review.safe_text('[链接]\n\n' + dotted_key), 'dotted key prefix leaked'
    letters_key = 'FixtureLettersOnly' * 8
    assert letters_key not in review.safe_text('请配置API_KEY="' + letters_key + '"，然后核对服务。'), 'Chinese-adjacent API key leaked'
    embedded_dotted = '.'.join(['Fixture9OnlyABC'] * 4)
    assert embedded_dotted not in review.safe_text('凭证 ' + embedded_dotted + ' 已配置'), 'embedded dotted key leaked'
    msg = {'openMessageId': 'fixture-stable-id', 'createTime': '2026-11-02 09:00:00',
           'sender': '演示同事', 'senderOpenDingTalkId': 'fixture-person', 'content': samples[1]}
    msg['quotedMessage'] = {**msg, 'content': long_key}
    msg['forwardMessages'] = [{**msg, 'content': samples[2]}]
    normalized = review.normalize_message(msg, {'openConversationId': 'fixture-chat', 'singleChat': True})
    assert normalized['openMessageId'] == msg['openMessageId']
    body = review.message_body(normalized)
    assert long_key not in body and bare_key not in body, 'quoted/forwarded credentials leaked'
    with tempfile.TemporaryDirectory(prefix='todo-redaction-') as tmp:
        project = pathlib.Path(tmp); root = project / 'data'; runs = project / 'work/runs'
        config = {'timeZone': 'Asia/Taipei', 'profile': 'fixture:person', 'earliestDate': '2026-10-01',
                  'dataDirectory': str(root), 'selfOpenDingTalkId': 'fixture-self'}
        run = review.start(runs, config, root, '2026-11-02T09:00:00+08:00')['run']
        response = {'success': True, 'result': {'hasMore': False, 'conversationMessagesList': [
            {'openConversationId': 'fixture-chat', 'title': '演示', 'singleChat': True, 'messages': [msg]}]}}
        review.ingest(runs, run, '0', response)
        shown = review.next_chunks(runs, run)['chunks']
        for f in [*(runs / run / 'pages').glob('*.json'), runs / run / 'messages.json', runs / run / 'chunks.json']:
            cached = f.read_text(encoding='utf-8')
            assert long_key not in cached and bare_key not in cached, 'credential persisted in source cache'
        assert long_key not in json.dumps(shown) and bare_key not in json.dumps(shown)
        review.acknowledge(runs, run, [{'id': c['id'], 'sha': c['sha'], 'note': '已完整阅读合成去敏消息并核对业务说明。'} for c in shown])
        review.stage_candidate(runs, run, {'summary': '合成去敏来源核对', 'tasks': [
            {'id': 'fixture-new', 'fields': {'title': '核对服务', 'detail': '核对配置后的服务。', 'category': 'action', 'priority': 2}, 'messageIndexes': [1]}]}, config)
        candidate = (runs / run / 'delta.json').read_text(encoding='utf-8')
        assert long_key not in candidate and bare_key not in candidate, 'credential leaked to source quote'
    print('PASS: long and embedded API credentials, URL-plus-key, nested sources, context and stable IDs')


def verify_remask_on_resume():
    """A run cached before a redaction fix is re-masked when resumed; unchanged chunks keep their reading notes."""
    leaked = 'Fixture9Only' * 4
    with tempfile.TemporaryDirectory(prefix='todo-remask-') as tmp:
        project = pathlib.Path(tmp); root = project / 'data'; runs = project / 'work/runs'
        config = {'timeZone': 'Asia/Taipei', 'profile': 'fixture:person', 'earliestDate': '2026-10-01',
                  'dataDirectory': str(root), 'selfOpenDingTalkId': 'fixture-self'}
        run = review.start(runs, config, root, '2026-10-21T09:00:00+08:00')['run']
        response = page('', False, '请核对演示事项。')
        response['result']['conversationMessagesList'].append({'openConversationId': 'key-chat', 'title': '配置群', 'singleChat': False,
            'messages': [{'openConversationId': 'key-chat', 'openMessageId': 'key-msg', 'createTime': '2026-10-20 08:30:00',
                          'sender': '演示同事', 'senderOpenDingTalkId': 'fixture-person', 'content': '测试值 ' + leaked + ' 配好后再确认。'}]})
        current = review.safe_text
        review.safe_text = lambda text: text if isinstance(text, str) else ''   # rules as they were before the fix
        try:
            review.ingest(runs, run, '0', response)
        finally:
            review.safe_text = current
        cache = runs / run
        assert leaked in (cache / 'chunks.json').read_text(encoding='utf-8'), 'fixture should start with a leaked cache'
        shown = review.next_chunks(runs, run, 2)['chunks']
        review.acknowledge(runs, run, [{'id': c['id'], 'sha': c['sha'], 'note': '已完整阅读合成消息并记录结论。'} for c in shown])
        clean_id = next(c['id'] for c in shown if c['conversationId'] == 'fixture-chat')
        resumed = review.start(runs, config, root, '2026-10-21T10:00:00+08:00')
        assert resumed['run'] == run
        for f in [*(cache / 'pages').glob('*.json'), cache / 'messages.json', cache / 'chunks.json']:
            assert leaked not in f.read_text(encoding='utf-8'), f'credential still cached in {f.name}'
        state = review.read(cache / 'review.json')
        assert state.get('remaskedAt') and list(state['reviewed']) == [clean_id], state['reviewed']
        again = review.next_chunks(runs, run)['chunks']
        assert [c['conversationId'] for c in again] == ['key-chat'] and leaked not in json.dumps(again, ensure_ascii=False)
        assert review.start(runs, config, root, '2026-10-21T11:00:00+08:00')['run'] == run
        assert review.read(cache / 'review.json')['remaskedAt'] == state['remaskedAt'], 'an already clean cache is left alone'
    print('PASS: resumed run re-masked with current rules, unchanged chunks keep notes, changed chunks re-read')


def verify_scope_and_groups():
    """Weekly full scan covers 7 days by default; groupScope "participated" skips groups I am not part of."""
    with tempfile.TemporaryDirectory(prefix='todo-scope-') as tmp:
        project = pathlib.Path(tmp); root = project / 'data'; runs = project / 'work/runs'
        config = {'timeZone': 'Asia/Taipei', 'profile': 'fixture:person', 'earliestDate': '2026-10-01',
                  'dataDirectory': str(root), 'selfOpenDingTalkId': 'fixture-self', 'groupScope': 'participated',
                  'selfNames': ['演示本人'], 'skipConversations': ['公告群']}
        followed = {'id': 'fixture-open', 'title': '等演示群回复', 'detail': '现状', 'category': 'waiting', 'priority': 1,
                    'sources': [{'chat': '跟进群', 'time': '2026-10-19 09:00', 'quote': '请回复', 'messageId': 'm0', 'conversationId': 'open-group'}]}
        inbox.merge(root, {'tasks': [followed]})
        run = review.start(runs, config, root, '2026-10-20T09:00:00+08:00', True)['run']
        state = review.read(runs / run / 'review.json')
        assert state['start'] == '2026-10-13T09:00:00+08:00' and state['mode'] == 'full', state['start']
        def group(cid, title, sender, content, single=False):
            return {'openConversationId': cid, 'title': title, 'singleChat': single, 'messages': [
                {'openConversationId': cid, 'openMessageId': cid + '-m', 'createTime': '2026-10-19 10:00:00',
                 'sender': '演示同事', 'senderOpenDingTalkId': sender, 'content': content}]}
        response = {'success': True, 'result': {'hasMore': False, 'conversationMessagesList': [
            group('direct', '演示同事', 'fixture-person', '请看一下', single=True),
            group('quiet', '闲聊群', 'fixture-person', '今天的午饭不错'),
            group('mention', '项目群', 'fixture-person', '@演示本人 请处理报价'),
            group('mine', '采购群', 'fixture-self', '我来跟进'),
            group('everyone', '通知群', 'fixture-person', '@所有人 周五前提交'),
            group('open-group', '跟进群', 'fixture-person', '已经回复了'),
            group('announce', '公告群', 'fixture-self', '我发的公告')]}}
        progress = review.ingest(runs, run, '0', response)
        chunks = review.read(runs / run / 'chunks.json')
        assert {c['conversationId'] for c in chunks} == {'direct', 'mention', 'mine', 'everyone', 'open-group'}, chunks
        state = review.read(runs / run / 'review.json')
        assert {g['conversationId'] for g in state['skippedConversations']} == {'quiet', 'announce'}
        assert progress['skippedGroups'] == 2 and progress['messages'] == 7
        assert review.skipped_note(state) == '按设置跳过2个未参与的群（共2条）；'
        wide = review.start(runs.parent / 'other', {**config, 'fullScanDays': 30, 'groupScope': 'all', 'skipConversations': []},
                            project / 'data2', '2026-10-20T09:00:00+08:00', True)
        assert wide['start'] == '2026-10-01T00:00:00+08:00', wide['start']
    print('PASS: 7-day weekly scan, configurable window, participated groups, @mentions, open-task groups, skip list')


def verify_redaction_gaps():
    """Shapes the first API-key fix still let through, and business text that must survive."""
    leaks = {'密钥：Fx12-ab34-CD56-ef78': 'Fx12-ab34-CD56-ef78', '请用 token=fixtureABC123 登录': 'fixtureABC123',
             'AKIAFIXTURE000000000 已配置': 'AKIAFIXTURE000000000',
             '凭证 FxAb1234+CdEf5678/GhIj9012+KlMn3456== 已发': 'FxAb1234+CdEf5678',
             '用 Fx7Kp2Lm9Qr4Ts6Vw8Za3Bc5 测试': 'Fx7Kp2Lm9Qr4Ts6Vw8Za3Bc5'}
    for text, secret in leaks.items():
        masked = review.safe_text(text)
        assert secret not in masked and review.safe_text(masked) == masked, (text, masked)
    for text in ['附件 2026年报-final-version-v2-revised-20261102.xlsx 看下', '订单 20261015-003 明天到',
                 'iPhone15ProMax 测试机型', '放在 /Users/demo/Documents/Codex/work/runs 里']:
        assert review.safe_text(text) == text, text
    print('PASS: labeled short keys, AWS keys, base64 and 20+ mixed keys; file names and business text kept')


def verify_login_pairs():
    """Unlabelled login material split over messages (account, then short password) never reaches caches or output."""
    account, password = 'fixture19@example.test', 'Aa12Bb34'   # reserved domain and generated value, not real
    secret = lambda value: account in json.dumps(value, ensure_ascii=False) or password in json.dumps(value, ensure_ascii=False)
    def msg(mid, time, content, sender='fixture-other', **extra):
        return {'openMessageId': mid, 'createTime': '2026-11-01 ' + time, 'sender': '演示同事', 'senderOpenDingTalkId': sender,
                'content': content, **extra}
    def response(messages, more=False, cursor=''):
        return {'success': True, 'result': {'hasMore': more, 'nextCursor': cursor, 'conversationMessagesList': [
            {'openConversationId': 'pair-chat', 'title': '演示同事', 'singleChat': True, 'messages': messages}]}}
    def fresh(tmp, name, verify=False):
        project = pathlib.Path(tmp) / name; root = project / 'data'; runs = project / 'work/runs'
        config = {'timeZone': 'Asia/Taipei', 'profile': 'fixture:person', 'earliestDate': '2026-10-01',
                  'dataDirectory': str(root), 'selfOpenDingTalkId': 'fixture-self', 'verifyMessagePages': verify}
        return runs, root, config, review.start(runs, config, root, '2026-11-01T14:00:00+08:00')['run']
    def cached(runs, run):
        files = [*(runs / run / 'pages').glob('*.json'), runs / run / 'messages.json', runs / run / 'chunks.json']
        return [review.read(f) for f in files if f.exists()]

    # Business text stays readable: a lone address, a lone model name, a sentence next to an address.
    assert review.inline_login('请把报价发到 ' + account) == [] and review.inline_login('iPhone15') == []
    alone = [review.normalize_message(m, {'openConversationId': 'c', 'singleChat': True}) for m in [
        msg('b1', '09:00:00', '我的邮箱 ' + account), msg('b2', '09:00:05', 'iPhone15 到了，订单 20261015-003'),
        msg('b3', '09:30:00', 'iPhone15')]]
    assert not review.mask_sequence(alone) and alone[0]['content'].endswith(account) and alone[2]['content'] == 'iPhone15'
    # Same message, and a login cue followed by a bare token.
    assert password not in review.mask_inline(account + ' ' + password) and account not in review.mask_inline(account + '\n' + password)
    cue = [review.normalize_message(m, {'openConversationId': 'c', 'singleChat': True}) for m in [
        msg('c1', '10:00:00', '后台登录用下面这个'), msg('c2', '10:00:04', password)]]
    assert review.mask_sequence(cue) and cue[1]['content'] == review.LOGIN_MASK and cue[0]['content'] == '后台登录用下面这个'
    # Quoted and forwarded parts of one message.
    nested = review.normalize_message(msg('n1', '11:00:00', '转给你', quotedMessage=msg('n0', '10:59:00', account),
                                          forwardMessages=[msg('n2', '10:59:04', password)]), {'openConversationId': 'c'})
    assert not secret(review.message_body(nested))
    with tempfile.TemporaryDirectory(prefix='todo-login-') as tmp:
        # Two messages, one page: nothing persisted, shown or staged; repeating the page is idempotent.
        runs, root, config, run = fresh(tmp, 'pair', verify=True)
        page = response([msg('m1', '13:56:05', account), msg('m2', '13:56:09', password)])
        review.ingest(runs, run, '0', page)
        assert not secret(review.read(runs / run / 'review.json')), 'page awaiting a second read is stored masked'
        review.ingest(runs, run, '0', page)
        assert not secret(cached(runs, run)) and not secret(review.read(runs / run / 'review.json'))
        shown = review.next_chunks(runs, run)['chunks']
        assert not secret(shown) and review.LOGIN_MASK in shown[0]['text']
        review.acknowledge(runs, run, [{'id': c['id'], 'sha': c['sha'], 'note': '已阅读合成的疑似登录材料，未记录原值。'} for c in shown])
        review.stage_candidate(runs, run, {'summary': '合成来源', 'tasks': [{'id': 'fixture-task', 'fields': {
            'title': '核对演示服务', 'detail': '合成回归。', 'category': 'action', 'priority': 2}, 'messageIndexes': [1, 2]}]}, config)
        assert not secret(review.read(runs / run / 'delta.json'))
        assert review.clean_cached(review.read(runs / run / 'messages.json')[0]) == review.read(runs / run / 'messages.json')[0]
        # Split across two pages: the earlier page is rewritten once the pair is complete; retrying it still matches.
        runs, root, config, run = fresh(tmp, 'pages')
        first = response([msg('p1', '13:56:05', account)], True, 'next')
        review.ingest(runs, run, '0', first)
        assert account in json.dumps(cached(runs, run), ensure_ascii=False), 'a lone address is kept until a pair appears'
        review.ingest(runs, run, 'next', response([msg('p2', '13:56:09', password)], True, 'last'))
        assert not secret(cached(runs, run)) and not secret(review.read(runs / run / 'review.json'))
        review.ingest(runs, run, '0', first)   # same cursor again: compared with what was fetched, not what was masked
        review.ingest(runs, run, 'last', response([msg('p3', '13:59:00', '收到，谢谢')]))
        assert not secret(cached(runs, run)) and not secret(review.next_chunks(runs, run)['chunks'])
        # A cache written before this fix: blocked at output, cleaned on resume, unchanged chunk keeps its note.
        runs, root, config, run = fresh(tmp, 'old')
        saved = review.mask_sequence, review.mask_message, review.scrub_pages
        review.mask_sequence, review.mask_message, review.scrub_pages = (lambda m: False), (lambda m: m), (lambda p, s: False)
        try:
            review.ingest(runs, run, '0', {'success': True, 'result': {'hasMore': False, 'nextCursor': '', 'conversationMessagesList': [
                {'openConversationId': 'a-chat', 'title': '演示', 'singleChat': True, 'messages': [msg('o0', '09:00:00', '请核对演示事项。')]},
                {'openConversationId': 'pair-chat', 'title': '演示同事', 'singleChat': True,
                 'messages': [msg('o1', '13:56:05', account), msg('o2', '13:56:09', password)]}]}})
        finally:
            review.mask_sequence, review.mask_message, review.scrub_pages = saved
        assert secret(cached(runs, run)), 'fixture should start with a leaked cache'
        clean = review.next_chunks(runs, run)['chunks']
        assert clean[0]['conversationId'] == 'a-chat'
        review.acknowledge(runs, run, [{'id': clean[0]['id'], 'sha': clean[0]['sha'], 'note': '已阅读合成的普通消息。'}])
        rejects(lambda: review.next_chunks(runs, run), 'leaked chunk must not be shown')
        assert review.start(runs, config, root, '2026-11-01T14:30:00+08:00')['run'] == run
        assert not secret(cached(runs, run))
        state = review.read(runs / run / 'review.json')
        assert list(state['reviewed']) == [clean[0]['id']] and state['remaskedAt']
        again = review.next_chunks(runs, run)['chunks']
        assert again[0]['conversationId'] == 'pair-chat' and not secret(again)
    print('PASS: login pairs in one message, across messages and pages, quoted/forwarded, after a login cue; old cache blocked then cleaned; business text kept')


def verify_redact_and_dingtalk_only():
    """A reader can hide a message the rules missed and keep going; runs no longer include the daily-report check."""
    hidden = 'HiddenPw9x'   # synthetic value the rules let through on its own
    with tempfile.TemporaryDirectory(prefix='todo-redact-') as tmp:
        project = pathlib.Path(tmp); root = project / 'data'; runs = project / 'work/runs'
        config = {'timeZone': 'Asia/Taipei', 'profile': 'fixture:person', 'earliestDate': '2026-10-01',
                  'dataDirectory': str(root), 'selfOpenDingTalkId': 'fixture-self'}
        run = review.start(runs, config, root, '2026-11-01T14:00:00+08:00')['run']
        state = review.read(runs / run / 'review.json')
        assert 'report' not in state['requiredChecks'], state['requiredChecks']
        def chat(cid, content):
            return {'openConversationId': cid, 'title': cid, 'singleChat': True, 'messages': [
                {'openConversationId': cid, 'openMessageId': cid + '-m', 'createTime': '2026-11-01 10:00:00',
                 'sender': '演示同事', 'senderOpenDingTalkId': 'fixture-person', 'content': content,
                 'forwardMessages': [{'openMessageId': cid + '-f', 'createTime': '2026-11-01 09:59:00', 'sender': '演示同事',
                                      'senderOpenDingTalkId': 'fixture-person', 'content': '转发里也有 ' + hidden}] if cid == 'b-chat' else []}]}
        review.ingest(runs, run, '0', {'success': True, 'result': {'hasMore': False, 'nextCursor': '',
                      'conversationMessagesList': [chat('a-chat', '请核对演示事项。'), chat('b-chat', '新的密钥是 ' + hidden)]}})
        first = review.next_chunks(runs, run)['chunks']
        review.acknowledge(runs, run, [{'id': c['id'], 'sha': c['sha'], 'note': '已阅读合成的普通消息。'} for c in first])
        rejects(lambda: review.redact(runs, run, [99]), 'unknown index')
        index = next(m['index'] for m in review.read(runs / run / 'messages.json') if m['openConversationId'] == 'b-chat')
        result = review.redact(runs, run, [index])
        assert result['hiddenMessages'] == 1 and len(result['reread']) == 1
        for f in [*(runs / run / 'pages').glob('*.json'), runs / run / 'messages.json', runs / run / 'chunks.json', runs / run / 'review.json']:
            assert hidden not in f.read_text(encoding='utf-8'), f.name
        state = review.read(runs / run / 'review.json')
        assert state['redacted'] == ['b-chat/b-chat-m'] and [c['id'] for c in first] == list(state['reviewed'])
        shown = review.next_chunks(runs, run)['chunks']
        assert shown[0]['conversationId'] == 'b-chat' and review.SENSITIVE_MASK in shown[0]['text']
        # A run started before the change still lists the daily-report check; resuming drops it.
        state['requiredChecks'].append('report'); review.save(runs / run / 'review.json', state)
        review.start(runs, config, root, '2026-11-01T14:30:00+08:00')
        assert 'report' not in review.read(runs / run / 'review.json')['requiredChecks']
        rejects(lambda: review.check_stage(runs, run, 'report', '日报服务已停用，不再核对。', True), 'retired check')
    print('PASS: redact hides a missed secret everywhere and keeps other notes; daily-report check retired')


if __name__ == '__main__':
    verify_redact_and_dingtalk_only()
    verify_login_pairs()
    verify_redaction_gaps()
    verify_scope_and_groups()
    verify_remask_on_resume()
    verify_api_redaction()
    verify_fixed_candidate()
    verify_utf8_cli()
    verify()
    verify_page_consistency()

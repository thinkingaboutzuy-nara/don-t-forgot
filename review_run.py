#!/usr/bin/env python3
"""Resumable local review ledger. Receives dws JSON; never invokes an external API.

start -> fetch-command / native dws -> next/ack -> stage decisions.json -> finish.
The fixed stage command preserves existing fields/sources and maps messageIndexes
to source IDs and direction. Decisions: summary, tasks [{id, fields?, appendDetail?,
messageIndexes?, sources?}], checks {requiredKey: note or {note, skipped}}, sync?.
Only finish may publish successful coverage. A partial run keeps its data and cursor.
"""
import argparse
import copy
import collections
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import pathlib
import re
import shlex
import shutil
import sys
from zoneinfo import ZoneInfo

HERE = pathlib.Path(__file__).resolve().parent
CHUNK_CHARS = 6000
STAGES = ['formal_todos', 'codex', 'report', 'handoffs']


class ReviewError(ValueError):
    pass


def need(ok, message):
    if not ok:
        raise ReviewError(message)


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def save(path, value):
    # Same atomic writer as inbox; one implementation of permissions/fsync/replace.
    from update_inbox import atomic
    atomic(path, value)


def stamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()


@contextlib.contextmanager
def locked(runs):
    runs.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (runs / '.review.lock').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def run_path(runs, key):
    need(isinstance(key, str) and re.fullmatch(r'[0-9]{8}-[0-9]{6}', key), '无效整理编号')
    return runs / key


def load_run(runs, key):
    path = run_path(runs, key)
    state = read(path / 'review.json')
    need(state['id'] == key and state['version'] == 1, '整理记录身份不一致')
    return path, state


def progress(state):
    return {'run': state['id'], 'phase': state['phase'], 'start': state['start'], 'end': state['end'],
            'mode': state['mode'], 'cursor': state['cursor'], 'pages': len(state['pages']),
            'messages': state.get('messageCount', 0), 'conversations': state.get('conversationCount', 0),
            'reviewed': len(state['reviewed']), 'chunks': len(state['chunks']),
            'remainingChecks': [k for k in state['requiredChecks'] if k not in state['checks']],
            'lastProgressAt': state['lastProgressAt'], 'attempts': state['attempts'],
            'lastError': state.get('lastError'),
            'pageVerification': {k: state.get('pendingPage', {}).get(k) for k in ['attempts', 'confirmations']}}


def start(runs, config, root, end=None, full=False):
    from update_inbox import open_view, parse_aware
    now = dt.datetime.now(ZoneInfo(config['timeZone'])).replace(microsecond=0)
    end = parse_aware(end, 'end').astimezone(ZoneInfo(config['timeZone'])) if end else now
    need(end <= now, '扫描终点不能在未来')
    identity = digest({'profile': config['profile'], 'root': str(root.resolve()), 'zone': config['timeZone']})
    with locked(runs):
        active = runs / '.active.json'
        if active.exists():
            path, state = load_run(runs, read(active)['run'])
            if state['phase'] != 'complete':
                need(state['identity'] == identity, '未完成整理的账号或数据目录不同，不能接错任务')
                state['attempts'] += 1
                state['lastStartedAt'] = stamp()
                save(path / 'review.json', state)
                return progress(state)
        view = open_view(root)
        sync = view.get('sync') or {}
        earliest = dt.datetime.fromisoformat(config['earliestDate']).replace(tzinfo=end.tzinfo)
        last = parse_aware(sync['scannedThrough'], 'scannedThrough') if sync.get('scannedThrough') else None
        last_full = parse_aware(sync['lastFullScan'], 'lastFullScan') if sync.get('lastFullScan') else None
        monday = end - dt.timedelta(days=end.weekday())
        monday = monday.replace(hour=0, minute=0, second=0)
        full = full or last is None or last_full is None or end - last_full > dt.timedelta(days=7) or (end.weekday() == 0 and end.hour >= 9 and last_full < monday)
        begin = max(earliest, end - dt.timedelta(days=30)) if full else max(earliest, last - dt.timedelta(minutes=15))
        need(begin <= end, '扫描起止时间倒置')
        key_time = now
        key = key_time.strftime('%Y%m%d-%H%M%S')
        path = run_path(runs, key)
        while path.exists():
            key_time += dt.timedelta(seconds=1)
            key = key_time.strftime('%Y%m%d-%H%M%S')
            path = run_path(runs, key)
        path.mkdir(mode=0o700)
        (path / 'pages').mkdir(mode=0o700)
        state = {'version': 1, 'id': key, 'identity': identity, 'root': str(root.resolve()),
                 'start': begin.isoformat(), 'end': end.isoformat(), 'mode': 'full' if full else 'incremental',
                 'baseline': sync.get('scannedThrough'), 'phase': 'fetch', 'cursor': '0', 'pages': [],
                 'chunks': [], 'reviewed': {}, 'presented': {}, 'checks': {},
                 'requiredChecks': STAGES + ['task:' + t['id'] for t in view['open']],
                 'attempts': 1, 'lastProgressAt': stamp(), 'createdAt': stamp(),
                 'verifyMessagePages': config.get('verifyMessagePages', False)}
        save(path / 'open-at-start.json', view)
        save(path / 'review.json', state)
        save(active, {'run': key})
        return progress(state)


def safe_text(text):
    """Discard credential-bearing bodies/URL signatures while keeping business text and IDs."""
    if not isinstance(text, str):
        return ''
    if re.search(r'(?:密码|口令|验证码|password|secret|bearer|private.key|access.token)', text, re.I) or re.fullmatch(r'\s*[0-9]{4,8}\s*', text):
        return '[登录材料已移除；需要业务依据时按消息ID另查]'
    text = re.sub(r'(?:https?://|dingtalk://)[^\s\]\)]+', '[链接]', text)
    if re.fullmatch(r'\s*(?:\d{8,20}\s+)?(?=[A-Za-z0-9]*[A-Za-z])(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{10,100}\s*', text):
        return '[登录材料或裸标识已隐藏，按消息ID核对]'
    if re.fullmatch(r'(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*[^A-Za-z0-9])[\x21-\x7e]{8,120}', text.strip()):
        return '[登录材料已移除]'
    # Attachment transport URLs and media IDs add no readable message content.
    text = re.sub(r'(?:https?://|dingtalk://)[^\s\]\)]+', '[链接]', text)
    text = re.sub(r'(?:mediaId|fileId|resourceId)\s*[:=]\s*[^\s\)]+', '[附件标识]', text)
    return text


def normalize_message(m, group):
    cid = m.get('openConversationId') or group.get('openConversationId')
    need(isinstance(cid, str) and cid and isinstance(m.get('openMessageId'), str), '消息缺少稳定ID')
    need(isinstance(m.get('createTime'), str) and m['createTime'], '消息缺少时间')
    result = {k: m.get(k, '') for k in ['openMessageId', 'createTime', 'sender', 'senderOpenDingTalkId']}
    result.update(openConversationId=cid, chatTitle=group.get('title') or group.get('conversationTitle') or '',
                  singleChat=group.get('singleChat'), content=safe_text(m.get('content', '')))
    quoted = m.get('quotedMessage')
    if quoted:
        result['quotedMessage'] = normalize_message(quoted, {'openConversationId': cid})
    if m.get('forwardMessages'):
        result['forwardMessages'] = [normalize_message(x, {'openConversationId': cid}) for x in m['forwardMessages']]
    return result


def message_body(m):
    body = m['content']
    if m.get('quotedMessage'):
        q = m['quotedMessage']
        body += '\n引用 ' + q['sender'] + ': ' + message_body(q)
    for f in m.get('forwardMessages', []):
        body += '\n转发 ' + f['createTime'] + ' ' + f['sender'] + ': ' + message_body(f)
    return body


def prepare(path, state):
    rows = {}
    for page in state['pages']:
        data = read(path / page['file'])
        need(digest(data) == page['sha'], '分页文件改变，停止覆盖审阅记录')
        for m in data['messages']:
            key = (m['openConversationId'], m['openMessageId'])
            if key in rows:
                need(rows[key] == m, '相同消息ID内容改变，需重新核对')
            rows[key] = m
    messages = sorted(rows.values(), key=lambda m: (m['createTime'], m['openConversationId'], m['openMessageId']))
    groups = collections.defaultdict(list)
    for index, m in enumerate(messages, 1):
        m['index'] = index
        groups[m['openConversationId']].append(m)
    chunks = []
    for cid, items in sorted(groups.items()):
        lines = ''.join(f"[{m['index']}] {m['createTime']} {m['sender']}: {message_body(m)}\n" for m in items)
        # Every character is covered, including an unusually long single message.
        for offset in range(0, len(lines), CHUNK_CHARS):
            text = lines[offset:offset + CHUNK_CHARS]
            chunk = {'id': f'chunk-{len(chunks) + 1:04}', 'conversationId': cid,
                     'title': next((m['chatTitle'] for m in reversed(items) if m['chatTitle']), ''),
                     'offset': offset, 'text': text}
            chunk['sha'] = digest(chunk)
            chunks.append(chunk)
    save(path / 'messages.json', messages)
    save(path / 'chunks.json', chunks)
    state.update(chunks=[{'id': c['id'], 'sha': c['sha']} for c in chunks], phase='review',
                 messageCount=len(messages), conversationCount=len(groups))


def ingest(runs, key, cursor, response):
    with locked(runs):
        path, state = load_run(runs, key)
        need(state['phase'] == 'fetch', '分页已完成，不能追加另一批边界的数据')
        need(response.get('success') is True, 'dws 未返回成功；保留原游标，不把失败当空页')
        data = response.get('result')
        need(isinstance(data, dict) and type(data.get('hasMore')) is bool, '分页响应缺少 hasMore')
        groups = data.get('conversationMessagesList')
        need(isinstance(groups, list), '分页响应缺少会话列表')
        need(all(isinstance(g, dict) and isinstance(g.get('messages'), list) for g in groups), '会话消息列表格式错误')
        messages = [normalize_message(m, g) for g in groups for m in g.get('messages', [])]
        begin = dt.datetime.fromisoformat(state['start']).replace(tzinfo=None)
        end = dt.datetime.fromisoformat(state['end']).replace(tzinfo=None)
        need(all(begin <= dt.datetime.fromisoformat(m['createTime']).replace(tzinfo=None) <= end for m in messages), '消息超出固定扫描边界')
        normalized = {'messages': messages, 'hasMore': data['hasMore'], 'nextCursor': str(data.get('nextCursor', ''))}
        hashed = digest(normalized)
        prior = next((p for p in state['pages'] if p['cursor'] == cursor), None)
        if prior:
            need(prior['sha'] == hashed, '重试同一页返回不同内容；保留已有记录并报告')
            return progress(state)
        need(cursor == state['cursor'], '游标与已保存进度不同，不能跳页')
        if data['hasMore']:
            nxt = normalized['nextCursor']
            need(nxt and nxt != cursor and all(p['cursor'] != nxt for p in state['pages']), '分页游标没有前进')
        if state.get('verifyMessagePages'):
            pending = state.get('pendingPage', {'cursor': cursor, 'attempts': 0, 'union': [], 'confirmations': 0})
            need(pending['cursor'] == cursor, '分页复核游标不同')
            merged = {(m['openConversationId'], m['openMessageId']): m for m in pending['union']}
            merged.update({(m['openConversationId'], m['openMessageId']): m for m in messages})
            complete_set = {(m['openConversationId'], m['openMessageId']) for m in messages} == set(merged)
            fingerprint = digest({'messages': sorted(messages, key=lambda m: (m['openConversationId'], m['openMessageId'])), 'hasMore': data['hasMore']})
            confirmations = (pending['confirmations'] + 1 if pending.get('fingerprint') == fingerprint else 1) if complete_set else 0
            pending.update(attempts=pending['attempts'] + 1, union=list(merged.values()),
                           fingerprint=fingerprint, confirmations=confirmations)
            # Same-cursor reads can silently omit a group half or even the final page.
            # Do not advance until two full observations agree; an empty continuation is never proof.
            if confirmations < 2 or (not messages and data['hasMore']):
                state['pendingPage'] = pending
                state['lastProgressAt'] = stamp()
                save(path / 'review.json', state)
                need(pending['attempts'] < 3, '同一游标3次仍未取得一致完整分页；保留已见消息和原游标，需核对来源，不得跳页')
                return progress(state)
            state.pop('pendingPage', None)
        name = f'pages/{len(state["pages"]):04}.json'
        save(path / name, normalized)
        receipt = {'file': name, 'sha': hashed, 'cursor': cursor}
        if state.get('verifyMessagePages'):
            receipt['verification'] = {'attempts': pending['attempts'], 'confirmations': pending['confirmations']}
        state['pages'].append(receipt)
        state['cursor'] = normalized['nextCursor'] if data['hasMore'] else None
        if not data['hasMore']:
            prepare(path, state)
        state['lastProgressAt'] = stamp()
        state.pop('lastError', None)
        save(path / 'review.json', state)
        return progress(state)


def next_chunks(runs, key, count=1):
    need(1 <= count <= 3, '一次只读1至3段，避免输出截断')
    with locked(runs):
        path, state = load_run(runs, key)
        need(state['phase'] == 'review', '先完成分页；已完成的轮次不重复审阅')
        chunks = read(path / 'chunks.json')
        pending = [c for c in chunks if c['id'] not in state['reviewed']][:count]
        expected = {c['id']: c['sha'] for c in state['chunks']}
        for c in pending:
            need(digest({k: v for k, v in c.items() if k != 'sha'}) == expected.get(c['id']) == c['sha'], '阅读文本改变')
            state['presented'][c['id']] = c['sha']
        save(path / 'review.json', state)
        return {'progress': progress(state), 'chunks': pending}


def acknowledge(runs, key, entries):
    with locked(runs):
        path, state = load_run(runs, key)
        need(state['phase'] == 'review', '当前轮次不在审阅阶段')
        for entry in entries:
            cid = entry['id']
            need(cid in state['presented'] and isinstance(entry.get('sha'), str) and state['presented'][cid] == entry['sha'], '只能确认已完整显示且哈希匹配的段落')
            need(isinstance(entry.get('note'), str) and 5 <= len(entry['note']) <= 2000, '保存具体结论或待核对事项，不能只写已读')
            state['reviewed'][cid] = {'sha': entry['sha'], 'note': entry['note'], 'at': stamp()}
        state['lastProgressAt'] = stamp()
        save(path / 'review.json', state)
        return progress(state)


def check_stage(runs, key, check, note, skipped=False):
    need(10 <= len(note) <= 3000, '核对说明须包含实际依据、结果与缺口')
    need(not skipped or check == 'report', '只有日报服务不可用可记为跳过，其他必需步骤不能略过')
    with locked(runs):
        path, state = load_run(runs, key)
        need(state['phase'] == 'review' and check in state['requiredChecks'], '无效核对项目或阶段')
        state['checks'][check] = {'note': note, 'skipped': skipped, 'at': stamp()}
        state['lastProgressAt'] = stamp()
        save(path / 'review.json', state)
        return progress(state)


def verify_identity(state, config):
    identity = digest({'profile': config['profile'], 'root': str(pathlib.Path(config['dataDirectory']).expanduser().resolve()), 'zone': config['timeZone']})
    need(state['identity'] == identity, '本轮账号、时区或数据目录已改变，不能接错任务')


def fetch_command(runs, key, config, project):
    """Print one native dws invocation. Never runs an API or a background loop."""
    _, state = load_run(runs, key)
    verify_identity(state, config)
    need(state['phase'] == 'fetch' and state['cursor'] is not None, '本轮分页已结束，按当前阶段继续审阅')
    zone = ZoneInfo(config['timeZone'])
    local = lambda value: dt.datetime.fromisoformat(value).astimezone(zone).strftime('%Y-%m-%d %H:%M:%S')
    native = ['dws', 'chat', 'message', 'list-all', '--start', local(state['start']), '--end', local(state['end']),
              '--limit', '100', '--cursor', state['cursor'], '--profile', config['profile'], '--format', 'json']
    receiver = [sys.executable, str(project.resolve() / 'review_run.py'), '--project', str(project.resolve()),
                'ingest', '--run', key, '--cursor', state['cursor']]
    return {'run': key, 'cursor': state['cursor'], 'command': 'set -o pipefail\n' + shlex.join(native) + ' | ' + shlex.join(receiver)}


def stage_candidate(runs, key, document, config):
    """Turn reviewed JSON decisions into a validated delta; never edits inbox/actions."""
    from update_inbox import validate_task
    with locked(runs):
        path, state = load_run(runs, key)
        verify_identity(state, config)
        need(state['phase'] == 'review' and state['cursor'] is None, '先完成分页与全文阅读')
        need(set(state['reviewed']) == {c['id'] for c in state['chunks']}, '先完整阅读本轮所有段落')
        need(isinstance(document, dict) and not (set(document) - {'tasks', 'checks', 'summary', 'sync'}), '判断JSON包含无效字段')
        summary = document.get('summary')
        need(isinstance(summary, str) and 1 <= len(summary) <= 2000, '提供本轮实际变化摘要')
        edits = document.get('tasks', [])
        checks = document.get('checks', {})
        sync = document.get('sync', {})
        need(isinstance(edits, list) and isinstance(checks, dict), 'tasks须为数组，checks须为对象')
        need(isinstance(sync, dict) and not (set(sync) - {'schedule', 'runThreadId'}) and all(isinstance(v, str) for v in sync.values()), 'sync只接受排程和原对话ID字符串')
        checked = {}
        for name, value in checks.items():
            need(name in state['requiredChecks'], '不存在的核对项目：' + name)
            need(isinstance(value, (str, dict)), '核对说明须为文字或note/skipped对象')
            if isinstance(value, dict):
                need(not (set(value) - {'note', 'skipped'}), '核对说明包含无效字段')
            note = value if isinstance(value, str) else value.get('note')
            skipped = False if isinstance(value, str) else value.get('skipped', False)
            need(isinstance(note, str) and 10 <= len(note) <= 3000, '核对说明须包含实际依据与结论')
            need(type(skipped) is bool and (not skipped or name == 'report'), '只有日报服务不可用可记为跳过')
            checked[name] = {'note': note, 'skipped': skipped, 'at': stamp()}
        root = pathlib.Path(state['root'])
        box = read(root / 'inbox.json') if (root / 'inbox.json').exists() else {'tasks': [], 'sync': {}}
        need(box.get('sync', {}).get('scannedThrough') == state['baseline'], '成功水位已变化，不能覆盖其他轮次')
        current = {t['id']: t for t in box['tasks']}
        rows = {m['index']: m for m in read(path / 'messages.json')}
        self_id = config.get('selfOpenDingTalkId')
        need(isinstance(self_id, str) and self_id, '缺少已核实的本人ID')
        allowed = {'title', 'detail', 'category', 'priority', 'due', 'waitingSince', 'needsConfirm', 'resolution', 'reportTodo', 'dispatch'}
        tasks, seen = [], set()
        for edit in edits:
            need(isinstance(edit, dict) and not (set(edit) - {'id', 'fields', 'appendDetail', 'messageIndexes', 'sources'}), '事项判断包含无效字段')
            ident = edit.get('id')
            need(isinstance(ident, str) and 1 <= len(ident) <= 160, '提供原事项或新事项的稳定ID')
            fields = edit.get('fields', {})
            need(isinstance(fields, dict) and not (set(fields) - allowed), 'fields包含无效事项字段')
            previous = current.get(ident)
            archived = next((a for a in box.get('archived', []) if a['id'] == ident), None)
            if previous is None and archived:
                previous = next((t for t in read(root / archived['file']) if t['id'] == ident), None)
                need(previous is not None, '原ID的封存事项不存在')
            task = copy.deepcopy(previous) if previous else {'id': ident, 'sources': []}
            task.update(fields)
            if 'appendDetail' in edit:
                extra = edit['appendDetail']
                need(isinstance(extra, str) and 'detail' not in fields, 'appendDetail须为文字，且不能同时覆盖detail')
                task['detail'] = '\n'.join(v for v in [task.get('detail', ''), extra] if v)
            indices, explicit = edit.get('messageIndexes', []), edit.get('sources', [])
            need(isinstance(indices, list) and isinstance(explicit, list), 'messageIndexes与sources须为数组')
            sources = copy.deepcopy(explicit)
            need(all(isinstance(s, dict) for s in sources), '每条来源须为对象')
            for index in indices:
                need(type(index) is int and index in rows, '引用了本轮不存在的消息索引')
                m = rows[index]
                kind = 'direct' if m['singleChat'] is True else 'group' if m['singleChat'] is False else 'unknown'
                recipient = ('我' if m['senderOpenDingTalkId'] != self_id else m['chatTitle']) if kind == 'direct' else m['chatTitle']
                sources.append({'chat': m['chatTitle'], 'time': m['createTime'], 'quote': message_body(m)[:600],
                                'messageId': m['openMessageId'], 'conversationId': m['openConversationId'],
                                'senderName': m['sender'], 'senderId': m['senderOpenDingTalkId'],
                                'recipientName': recipient, 'conversationType': kind})
            source_keys = {(s['conversationId'], s['messageId']) for s in task['sources']}
            for source in sources:
                need(all(isinstance(source.get(k), str) for k in ['chat', 'time', 'quote', 'messageId', 'conversationId']), '来源缺少稳定身份、时间或正文')
                source['quote'] = safe_text(source['quote'])[:600]
                source_key = (source['conversationId'], source['messageId'])
                if source_key not in source_keys:
                    task['sources'].append(source); source_keys.add(source_key)
            validate_task(task, seen)
            tasks.append(task)
        payload = {'tasks': tasks, 'sync': {**sync, 'message': summary}}
        # Validate the entire document before recording anything. Crash/retry is idempotent.
        save(path / 'delta.json', payload)
        state['checks'].update(checked)
        state['candidateHash'] = digest(payload)
        state['lastProgressAt'] = stamp()
        save(path / 'review.json', state)
        return {'progress': progress(state), 'candidate': str(path / 'delta.json'), 'taskCount': len(tasks),
                'taskIds': [t['id'] for t in tasks], 'candidateHash': state['candidateHash']}


def validate_ready(runs, key, sync, old_sync):
    path, state = load_run(runs, key)
    need(state['phase'] == 'review' and state['cursor'] is None, '尚未完成分页')
    if state.get('verifyMessagePages'):
        need(all(p.get('verification', {}).get('confirmations', 0) >= 2 for p in state['pages']), '尚有未经两次一致复核的分页')
    need(set(state['reviewed']) == {c['id'] for c in state['chunks']}, '尚有未审阅的聊天段落')
    need(all(state['reviewed'][c['id']]['sha'] == c['sha'] for c in state['chunks']), '审阅内容身份不一致')
    need(all(k in state['checks'] for k in state['requiredChecks']), '尚有未完成的待办或跨系统核对')
    need(sync.get('scannedThrough') == state['end'] and sync.get('mode') == state['mode'], '成功水位或模式与本轮边界不同')
    need(old_sync.get('scannedThrough') == state['baseline'], '成功水位已被其他整理推进，停止覆盖')
    return state


def record_failure(runs, key, reason):
    from update_inbox import merge
    need(10 <= len(reason) <= 1000, '失败说明须写清实际错误及下一动作')
    with locked(runs):
        path, state = load_run(runs, key)
        need(state['phase'] != 'complete', '已成功轮次不能重新标记失败')
        state['lastError'] = {'at': stamp(), 'reason': reason, 'phase': state['phase']}
        save(path / 'review.json', state)
        sync = {'state': 'partial', 'mode': state['mode'],
                'scope': f"{state['start']} 至 {state['end']}；已保存{len(state['pages'])}页，已审阅{len(state['reviewed'])}/{len(state['chunks'])}段；进度保留。",
                'message': f"整理未完成：{reason}；下次从原进度继续。"}
        merge(pathlib.Path(state['root']), {'tasks': [], 'sync': sync})
        return progress(state)


def finish(runs, key, payload, message):
    from update_inbox import merge
    with locked(runs):
        path, state = load_run(runs, key)
        root = pathlib.Path(state['root'])
        original_hash = digest(payload)
        need(not state.get('candidateHash') or state['candidateHash'] == original_hash, '候选在登记后改变，须重新stage核对，不能直接合并')
        old = read(root / 'inbox.json') if (root / 'inbox.json').exists() else {'tasks': [], 'sync': {}}
        # Recover a crash after inbox replace but before the ledger was marked complete.
        already = old.get('sync', {}).get('reviewRunId') == key
        if already:
            need(state.get('preparedPayloadHash') == original_hash, '该轮已发布，不能用另一份增量重放')
            need(old['sync'].get('scannedThrough') == state['end'], '已发布轮次水位不一致')
            result = {'recovered': True, 'taskCount': len(old['tasks']), 'sync': old['sync']}
        else:
            skipped = [v['note'] for v in state['checks'].values() if v.get('skipped')]
            sync = {**payload.get('sync', {}), 'state': 'ok', 'mode': state['mode'],
                    'scannedThrough': state['end'], 'reviewRunId': key,
                    'scope': f"{state['start']} 至 {state['end']}；{len(state['pages'])}页、{state.get('messageCount', 0)}条、{state.get('conversationCount', 0)}个会话；{len(state['chunks'])}段全文及全部必需核对已完成；图片、语音、文件仅按影响结论的需要解析。",
                    'message': message + ('；日报同步跳过：' + '；'.join(skipped) if skipped else '')}
            validate_ready(runs, key, sync, old.get('sync', {}))
            state['preparedPayloadHash'] = original_hash
            save(path / 'review.json', state)
            result = merge(root, {**payload, 'sync': sync}, review_root=runs)
            saved = read(root / 'inbox.json')
            need(saved['sync'] == result['sync'], '发布后水位读回不一致')
            by_id = {t['id']: t for t in saved['tasks']}
            for task in payload.get('tasks', []):
                need(all(by_id.get(task['id'], {}).get(k) == v for k, v in task.items()
                         if k not in ['firstSeen', 'lastSeen', 'closedAt']), '发布后事项同ID读回不一致')
        state.update(phase='complete', completedAt=stamp(), lastProgressAt=stamp())
        save(path / 'review.json', state)
        save(path / 'result.json', result)
        message_file = path / 'messages.json'
        if message_file.exists():
            rows = read(message_file)
            save(path / 'message-ids.json', [{'conversationId': m['openConversationId'], 'messageId': m['openMessageId'], 'time': m['createTime']} for m in rows])

        # Successful review only: raw/normalized message bodies are no longer needed.
        for name in ['pages', 'chunks.json', 'messages.json', 'open-at-start.json'] + [p.name for p in path.glob('target-*.json')]:
            target = path / name
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
        prune_completed(runs)
        return result


def prune_completed(runs, keep=6):
    # Never delete an unfinished or unknown directory.
    completed = []
    for candidate in runs.iterdir():
        receipt = candidate / 'review.json'
        if candidate.is_dir() and receipt.is_file():
            try:
                state = read(receipt)
                if state.get('phase') == 'complete':
                    completed.append((state.get('completedAt', ''), candidate))
            except (ValueError, OSError):
                continue
    for _, candidate in sorted(completed, reverse=True)[keep:]:
        shutil.rmtree(candidate)


def main():
    # Scheduler locale must not change the JSON wire/file encoding.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=pathlib.Path, default=HERE)
    sub = parser.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('start'); p.add_argument('--end'); p.add_argument('--full', action='store_true')
    for cmd in ['status', 'fetch-command', 'ingest', 'next', 'ack', 'check', 'stage', 'finish', 'fail']:
        p = sub.add_parser(cmd); p.add_argument('--run', required=True)
        if cmd == 'ingest': p.add_argument('--cursor', required=True)
        if cmd == 'next': p.add_argument('--count', type=int, default=1)
        if cmd in ['ack', 'stage', 'finish']: p.add_argument('--input', type=pathlib.Path, required=cmd != 'finish')
        if cmd == 'check':
            p.add_argument('--key', required=True); p.add_argument('--note', required=True); p.add_argument('--skipped', action='store_true')
        if cmd == 'finish': p.add_argument('--message')
        if cmd == 'fail': p.add_argument('--reason', required=True)
    args = parser.parse_args(); runs = args.project / 'work/runs'
    try:
        if args.cmd == 'start':
            config = read(args.project / 'config.json')
            value = start(runs, config, pathlib.Path(config['dataDirectory']).expanduser(), args.end, args.full)
        elif args.cmd == 'status': value = progress(load_run(runs, args.run)[1])
        elif args.cmd == 'fetch-command': value = fetch_command(runs, args.run, read(args.project / 'config.json'), args.project)
        elif args.cmd == 'ingest': value = ingest(runs, args.run, args.cursor, json.load(sys.stdin))
        elif args.cmd == 'next': value = next_chunks(runs, args.run, args.count)
        elif args.cmd == 'ack': value = acknowledge(runs, args.run, read(args.input))
        elif args.cmd == 'check': value = check_stage(runs, args.run, args.key, args.note, args.skipped)
        elif args.cmd == 'stage': value = stage_candidate(runs, args.run, read(args.input), read(args.project / 'config.json'))
        elif args.cmd == 'fail': value = record_failure(runs, args.run, args.reason)
        else:
            payload = read(args.input or run_path(runs, args.run) / 'delta.json')
            value = finish(runs, args.run, payload, args.message or payload.get('sync', {}).get('message', '本轮整理已完成'))
    except (ReviewError, ValueError, OSError) as e:
        raise SystemExit(f'整理未完成，原进度保留：{e}')
    print(json.dumps(value, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

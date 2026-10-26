#!/usr/bin/env python3
"""Validate and atomically merge an agent-produced task delta. Never edits actions.json.

--dump     full inbox + actions
--open     compact view for a run: open tasks (with the user's own deadline, if set), user feedback, hand-offs,
           pending report-platform ticks, sync
--input    merge a delta; closed tasks older than --archive-days move to archive/
"""
import argparse, datetime as dt, fcntl, json, os, pathlib, sys, tempfile

CONFIG = pathlib.Path(__file__).with_name('config.json')
DEFAULT = (pathlib.Path(json.loads(CONFIG.read_text(encoding='utf-8'))['dataDirectory']).expanduser() if CONFIG.exists()
           else pathlib.Path.home() / 'Library/Application Support/DingTalkTodo')
CATEGORIES = ['action', 'waiting', 'resolved', 'stale']
CLOSED_CATEGORIES = ['resolved', 'stale']


class DeltaError(ValueError):
    pass


def check(cond, message):
    # Explicit errors instead of assert: validation must survive `python -O`.
    if not cond:
        raise DeltaError(message)


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def parse_aware(value, field):
    try:
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (AttributeError, ValueError):
        raise DeltaError(f'{field} 不是 ISO 8601 时间：{value!r}')
    check(parsed.tzinfo is not None, f'{field} 必须带时区：{value!r}')
    return parsed


def read(path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding='utf-8'))


def atomic(path, obj):
    fd, name = tempfile.mkstemp(prefix='.' + path.stem + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def effective_status(task, action, now):
    """Mirror of TodoStore.status in TodoWindow.swift. Keep both in sync."""
    auto = 'done' if task['category'] == 'resolved' else 'dismissed' if task['category'] == 'stale' else 'active'
    status = (action or {}).get('status')
    if not status:
        return auto
    if status == 'snoozed':
        return 'snoozed' if (action.get('until') or 0) > now.timestamp() else auto
    if platform_newer(task, action):
        return auto
    return status


def platform_newer(task, action):
    """A tick or untick seen on the report platform after the manual action wins."""
    changed = (task.get('reportTodo') or {}).get('changedAt')
    if not changed or not action.get('updatedAt'):
        return False
    try:
        return parse_aware(changed, 'reportTodo.changedAt') > parse_aware(action['updatedAt'], 'updatedAt')
    except DeltaError:
        return False


def closed_at(task, action):
    if action and action.get('status') in ['done', 'dismissed']:
        return action.get('updatedAt')
    return task.get('closedAt') or task.get('lastSeen')


def validate_task(t, seen):
    check(isinstance(t, dict), '每个 task 必须是对象')
    key = t.get('id')
    check(isinstance(key, str) and 1 <= len(key) <= 160, f'无效 id：{key!r}')
    check(key not in seen, f'增量内重复 id：{key}')
    seen.add(key)
    for field in ['title', 'detail', 'category', 'priority', 'sources']:
        check(field in t, f'{key} 缺少 {field}')
    check(t['category'] in CATEGORIES, f'{key} 的 category 无效：{t["category"]!r}')
    check(t['priority'] in [0, 1, 2], f'{key} 的 priority 无效')
    check(isinstance(t['title'], str) and 1 <= len(t['title']) <= 140, f'{key} 的 title 长度无效')
    check(isinstance(t['detail'], str) and len(t['detail']) <= 3000, f'{key} 的 detail 过长')
    check(isinstance(t['sources'], list) and t['sources'], f'{key} 必须有依据')
    for s in t['sources']:
        for field in ['chat', 'time', 'quote', 'messageId', 'conversationId']:
            check(isinstance(s.get(field), str), f'{key} 的来源缺少 {field}')
        check(len(s['quote']) <= 600, f'{key} 的来源引用超过 600 字')
    if t.get('due'):
        parse_aware(t['due'], f'{key}.due')
    if t.get('waitingSince'):
        parse_aware(t['waitingSince'], f'{key}.waitingSince')
    if 'needsConfirm' in t:
        check(isinstance(t['needsConfirm'], bool), f'{key}.needsConfirm 必须是布尔值')
    if 'dispatch' in t:
        check(t['dispatch'] is None or isinstance(t['dispatch'], dict), f'{key}.dispatch 必须是对象')
    if t.get('reportTodo') is not None:
        r = t['reportTodo']
        check(isinstance(r, dict) and isinstance(r.get('id'), str) and r['id'], f'{key}.reportTodo 须有平台待办 id')
        check(type(r.get('version')) is int and type(r.get('done')) is bool, f'{key}.reportTodo 须有整数 version 与布尔 done')
        if r.get('changedAt'):
            parse_aware(r['changedAt'], f'{key}.reportTodo.changedAt')
    if t['category'] in CLOSED_CATEGORIES:
        check(bool(t.get('resolution')), f'{key} 标为 {t["category"]} 时必须写 resolution')
    return key


def archive_closed(root, inbox, actions, now, days):
    """Move tasks closed for more than `days` into archive/YYYY-MM.json; keep a small index."""
    cutoff = now - dt.timedelta(days=days)
    keep, moved = [], {}
    for t in inbox['tasks']:
        action = actions.get(t['id'])
        when = closed_at(t, action)
        if effective_status(t, action, now) in ['done', 'dismissed'] and when:
            try:
                closed = parse_aware(when, 'closedAt')
            except DeltaError:
                closed = None
            if closed and closed < cutoff:
                moved.setdefault(closed.strftime('%Y-%m'), []).append({**t, 'closedAt': when})
                continue
        keep.append(t)
    if not moved:
        return 0
    (root / 'archive').mkdir(mode=0o700, exist_ok=True)
    index = inbox.setdefault('archived', [])
    for month, rows in moved.items():
        path = root / 'archive' / f'{month}.json'
        existing = {r['id']: r for r in read(path, [])}
        existing.update({r['id']: r for r in rows})
        atomic(path, list(existing.values()))
        index.extend({'id': r['id'], 'title': r['title'], 'closedAt': r['closedAt'], 'file': f'archive/{month}.json'}
                     for r in rows)
    inbox['tasks'] = keep
    return sum(len(r) for r in moved.values())


def restore_archived(root, inbox, key):
    index = inbox.get('archived', [])
    ref = next((r for r in index if r['id'] == key), None)
    if not ref:
        return {}
    inbox['archived'] = [r for r in index if r['id'] != key]
    return next((r for r in read(root / ref['file'], []) if r['id'] == key), {})


def merge(root, payload, archive_days=30, review_root=None):
    if review_root is None and root.resolve() == DEFAULT.resolve() and read(CONFIG, {}).get("requireReviewReceipt"):
        review_root = CONFIG.parent / "work/runs"
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    with (root / 'inbox.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        old = read(root / 'inbox.json', {'schemaVersion': 1, 'updatedAt': None, 'tasks': [], 'sync': {}})
        check(old.get('schemaVersion') == 1, '不支持的 inbox schemaVersion')
        actions = read(root / 'actions.json', {})
        now = utcnow()
        stamp = now.isoformat()
        inbox = json.loads(json.dumps(old))
        rows = {t['id']: t for t in inbox['tasks']}
        incoming = payload.get('tasks', [])
        seen = set()
        for t in incoming:
            key = validate_task(t, seen)
            # Stable identity is mandatory: an exact title under a new ID means the agent lost track.
            titles = [(o['id'], o['title']) for o in rows.values()] + [(r['id'], r['title']) for r in inbox.get('archived', [])]
            check(all(i == key or title != t['title'] for i, title in titles), f'标题已存在，请复用原 ID：{t["title"]}')
            base = rows.get(key) or restore_archived(root, inbox, key)
            merged = {**base, **t, 'firstSeen': base.get('firstSeen', stamp), 'lastSeen': stamp}
            if t['category'] in CLOSED_CATEGORIES and base.get('category') not in CLOSED_CATEGORIES:
                merged['closedAt'] = stamp
            elif t['category'] not in CLOSED_CATEGORIES:
                merged.pop('closedAt', None)
            rows[key] = merged
        inbox['tasks'] = list(rows.values())

        delta_sync = payload.get('sync', {})
        check(isinstance(delta_sync, dict), 'sync 必须是对象')
        sync = {**old.get('sync', {}), **delta_sync}
        # Match SyncInfo's optional strings before replacing a readable inbox.
        for field in ['state', 'message', 'scannedThrough', 'scope', 'schedule', 'mode', 'runThreadId']:
            check(sync.get(field) is None or isinstance(sync[field], str), f'sync.{field} 必须是字符串或 null')
        check(sync.get('state', 'ok') in ['ok', 'partial', 'error'], 'sync.state 无效')
        if sync.get('mode') is not None:
            check(sync['mode'] in ['incremental', 'full'], 'sync.mode 只能是 incremental 或 full')
        if delta_sync.get('state') in ['partial', 'error']:
            # Failed coverage cannot advance the last successful scan, even if a delta tries to.
            if old.get('sync', {}).get('scannedThrough'):
                sync['scannedThrough'] = old['sync']['scannedThrough']
            else:
                sync.pop('scannedThrough', None)
        if delta_sync.get('state') == 'ok' or ('scannedThrough' in delta_sync and delta_sync.get('state') not in ['partial', 'error']):
            if review_root is not None:
                from review_run import validate_ready, ReviewError
                try:
                    receipt = validate_ready(review_root, sync.get('reviewRunId'), sync, old.get('sync', {}))
                except (ReviewError, OSError) as error:
                    raise DeltaError(str(error)) from error
                check(receipt['root'] == str(root.resolve()), '审阅记录属于另一个数据目录')
            check(sync.get('scannedThrough') and sync.get('scope'), '成功整理必须写 scannedThrough 与 scope')
            scanned = parse_aware(sync['scannedThrough'], 'sync.scannedThrough')
            previous = old.get('sync', {}).get('scannedThrough')
            if previous:
                check(scanned >= parse_aware(previous, '旧 scannedThrough'), '扫描水位不能倒退')
            if sync.get('mode') == 'full':
                sync['lastFullScan'] = sync['scannedThrough']
        inbox.update({'schemaVersion': 1, 'updatedAt': stamp, 'sync': sync})
        archived = archive_closed(root, inbox, actions, now, archive_days)
        atomic(root / 'inbox.json', inbox)
        return {'taskCount': len(inbox['tasks']), 'upserted': len(incoming), 'archived': archived, 'sync': sync}


def open_view(root):
    """What a run needs to read instead of the full inbox."""
    inbox = read(root / 'inbox.json', {'tasks': [], 'sync': {}})
    actions = read(root / 'actions.json', {})
    now = utcnow()
    open_tasks, feedback, handoffs, report_sync = [], [], [], []
    for t in inbox['tasks']:
        action = actions.get(t['id'])
        status = effective_status(t, action, now)
        link = t.get('reportTodo')
        # Push a manual tick/untick to the platform unless the platform changed it more recently.
        if link and action and action.get('status') in ['done', 'active'] and not platform_newer(t, action):
            want = action['status'] == 'done'
            if want != link['done']:
                report_sync.append({'id': t['id'], 'todoId': link['id'], 'expectedVersion': link['version'],
                                    'done': want, 'at': action.get('updatedAt')})
        if status in ['active', 'snoozed']:
            row = {
                'id': t['id'], 'title': t['title'], 'category': t['category'], 'priority': t['priority'],
                'due': t.get('due'), 'status': status, 'needsConfirm': t.get('needsConfirm', t['title'].startswith('待确认')),
                'lastEvidence': max((s['time'] for s in t['sources']), default=None),
                'conversationIds': sorted({s['conversationId'] for s in t['sources']}),
                'dispatch': t.get('dispatch'), 'reportTodo': t.get('reportTodo'),
            }
            # The user's own deadline (TodoStore.due shows it first). Runs keep writing `due` from chat and never touch it.
            if action and action.get('dueOverride'):
                row['dueOverride'] = action['dueOverride']
            open_tasks.append(row)
        if action and action.get('status') in ['done', 'dismissed']:
            feedback.append({'id': t['id'], 'title': t['title'], 'status': action['status'],
                             'reason': action.get('reason'), 'note': action.get('note'), 'at': action.get('updatedAt')})
        if action and action.get('handoffAt') and not (t.get('dispatch') or {}).get('threadId'):
            handoffs.append({'id': t['id'], 'title': t['title'], 'handoffAt': action['handoffAt']})
    feedback.sort(key=lambda f: f.get('at') or '', reverse=True)
    return {'sync': inbox.get('sync', {}), 'open': open_tasks, 'feedback': feedback[:40],
            'handoffs': handoffs, 'reportSync': report_sync, 'archivedCount': len(inbox.get('archived', []))}


def main():
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        stream.reconfigure(encoding='utf-8')
    p = argparse.ArgumentParser()
    p.add_argument('--data-dir', type=pathlib.Path, default=DEFAULT)
    p.add_argument('--input', type=pathlib.Path)
    p.add_argument('--dump', action='store_true')
    p.add_argument('--open', action='store_true')
    p.add_argument('--archive-days', type=int, default=30)
    a = p.parse_args()
    if a.dump:
        print(json.dumps({'inbox': read(a.data_dir / 'inbox.json', {}), 'actions': read(a.data_dir / 'actions.json', {})},
                         ensure_ascii=False, indent=2))
        return
    if a.open:
        print(json.dumps(open_view(a.data_dir), ensure_ascii=False, indent=2))
        return
    if not a.input:
        p.error('--input、--dump 或 --open 必选其一')
    try:
        config = read(CONFIG, {})
        review_root = (CONFIG.parent / 'work/runs' if config.get('requireReviewReceipt')
                       and a.data_dir.resolve() == DEFAULT.resolve() else None)
        result = merge(a.data_dir, json.loads(a.input.read_text(encoding='utf-8')), a.archive_days, review_root=review_root)
    except DeltaError as e:
        raise SystemExit(f'增量被拒绝，清单未改动：{e}')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

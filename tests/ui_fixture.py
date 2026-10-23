#!/usr/bin/env python3
"""Write a synthetic data directory for tests/ui_snapshot.py. Dates are relative to now; nothing comes from live data.

python3 tests/ui_fixture.py <empty dir>

Covers: 今天优先 with an own deadline (“已改”), 本周, 等回复 past 3 days (“催一下”), 待确认, a snoozed card, an archived card.
"""
import datetime as dt
import json
import pathlib
import sys

TZ = dt.timezone(dt.timedelta(hours=8))


def main(out):
    out.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(TZ).replace(microsecond=0)
    day = lambda n, h=18: (now + dt.timedelta(days=n)).replace(hour=h, minute=0, second=0).isoformat()
    local = lambda n: (now + dt.timedelta(days=n)).strftime('%Y-%m-%d %H:%M')

    def task(id, title, category='action', priority=1, due=None, detail='合成演示数据。', **extra):
        source = {'chat': '演示群', 'time': local(-1), 'quote': f'演示：{title}', 'messageId': f'm-{id}',
                  'conversationId': 'demo', 'senderName': '演示同事', 'senderId': 'u-demo', 'recipientName': '我'}
        return {'id': id, 'title': title, 'detail': detail, 'category': category, 'priority': priority, 'due': due,
                'sources': [source], 'firstSeen': day(-5, 9), 'lastSeen': now.isoformat(), **extra}

    waiting = task('demo-wait', '等演示同事确认到货', 'waiting', waitingSince=day(-4, 10))
    waiting['sources'][0].update(time=local(-4), senderName='我', senderId='self', recipientName='演示同事', quote='到货了麻烦确认一下，谢谢')
    tasks = [
        task('demo-urgent', '提交演示报销单', priority=0, due=day(0)),
        task('demo-moved', '整理演示周报', due=day(3), detail='整理给的期限是三天后，这里改成了今天。'),
        task('demo-week', '核对演示清单', due=day(4), firstSeen=now.isoformat()),
        waiting,
        task('demo-wait-new', '等演示同事回复报价', 'waiting', waitingSince=day(-1, 10)),
        task('demo-confirm', '待确认：演示线索', needsConfirm=True),
        task('demo-snoozed', '回电演示客户'),
        task('demo-done', '已发布的演示版本', 'resolved', resolution='合成数据：已完成'),
    ]
    actions = {
        'demo-moved': {'updatedAt': now.isoformat(), 'dueOverride': (now + dt.timedelta(hours=2)).isoformat()},
        'demo-snoozed': {'status': 'snoozed', 'until': (now + dt.timedelta(hours=1)).timestamp(), 'updatedAt': now.isoformat()},
    }
    inbox = {'schemaVersion': 1, 'updatedAt': now.isoformat(), 'tasks': tasks,
             'sync': {'state': 'ok', 'scannedThrough': now.isoformat(), 'scope': '合成数据', 'mode': 'incremental',
                      'message': '合成数据，仅供截图。'}}
    (out / 'inbox.json').write_text(json.dumps(inbox, ensure_ascii=False, indent=2))
    (out / 'actions.json').write_text(json.dumps(actions, ensure_ascii=False, indent=2))
    print(out)


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(pathlib.Path(sys.argv[1]))

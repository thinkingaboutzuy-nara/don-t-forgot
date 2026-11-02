#!/usr/bin/env python3
"""Write a synthetic data directory for tests/ui_snapshot.py. Dates are relative to now; nothing comes from live data.

python3 tests/ui_fixture.py <empty dir> [--extra N]

--extra N adds N more ordinary items spread over the groups, to check behaviour with a long list.

Covers: 今天优先 with an own deadline (“已改”) and several updates, 本周, 等回复 past 3 days (“催一下”), 待确认, a snoozed card,
an archived card, and a run record (one added, one closed by the run).
"""
import datetime as dt
import json
import pathlib
import sys

TZ = dt.timezone(dt.timedelta(hours=8))


def main(out, extra=0):
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
        task('demo-moved', '整理演示周报', due=day(3), detail='10月23日承诺先整理一版演示周报。\n'
             '25日同事补充了两项数据，口径仍待确认。\n整理给的期限是三天后，这里改成了今天；数据口径已确认，只差汇总。'),
        task('demo-week', '核对演示清单', due=day(4), firstSeen=now.isoformat()),
        waiting,
        task('demo-wait-new', '等演示同事回复报价', 'waiting', waitingSince=day(-1, 10)),
        task('demo-confirm', '待确认：演示线索', needsConfirm=True),
        task('demo-snoozed', '回电演示客户'),
        task('demo-done', '已发布的演示版本', 'resolved', resolution='合成数据：已完成', closedAt=now.isoformat()),
    ]
    groups = [dict(category='action', priority=0), dict(category='action', due=day(5)), dict(category='waiting', waitingSince=day(-2, 10)),
              dict(category='action', needsConfirm=True)]
    for i in range(extra):
        kind = groups[i % len(groups)]
        tasks.append(task(f'demo-extra-{i}', f'演示事项 {i + 1}', detail='第一段经过。\n第二段经过，内容稍长一些，用来撑高卡片。\n最新现状：还差一步。', **kind))
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
    args = sys.argv[1:]
    if len(args) not in (1, 3) or (len(args) == 3 and args[1] != '--extra'):
        raise SystemExit(__doc__)
    main(pathlib.Path(args[0]), int(args[2]) if len(args) == 3 else 0)

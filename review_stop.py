#!/usr/bin/env python3
"""Continue only the Codex session bound to an unfinished local review.

No network calls, tool execution, message acknowledgements or business decisions.
The hook reads the ledger and uses Codex's Stop continuation contract.
"""
import argparse
import datetime as dt
import json
import pathlib
import shlex
import sys

import review_run as review


def next_step(project, state):
    command = f'python3 {shlex.quote(str(project / "review_run.py"))}'
    run = shlex.quote(state['id'])
    if state['phase'] == 'fetch':
        return f'{command} fetch-command --run {run}'
    if len(state['reviewed']) < len(state['chunks']):
        return f'{command} next --run {run} --count 1'
    missing = [k for k in state['requiredChecks'] if k not in state['checks']]
    if missing:
        return '完成实际核对并stage原decisions.json；尚缺：' + '、'.join(missing)
    return f'{command} finish --run {run}'


def verified_complete(path, state):
    if state['phase'] != 'complete' or not (path / 'result.json').is_file():
        return False
    result = review.read(path / 'result.json')
    sync = result.get('sync', {})
    inbox = review.read(pathlib.Path(state['root']) / 'inbox.json').get('sync', {})
    return (sync.get('state') == 'ok' and sync.get('reviewRunId') == state['id']
            and sync.get('scannedThrough') == state['end']
            and inbox.get('state') == 'ok'
            and dt.datetime.fromisoformat(inbox['scannedThrough']) >= dt.datetime.fromisoformat(state['end'])
            and set(state['reviewed']) == {c['id'] for c in state['chunks']}
            and set(state['checks']) >= set(state['requiredChecks']))


def evidenced_blocker(path, state, session_id):
    blocker = state.get('stopBlocker')
    if not blocker or blocker.get('sessionId') != session_id or blocker.get('fingerprint') != review.work_fingerprint(state):
        return False
    evidence = (path / blocker['file']).resolve()
    return evidence.is_relative_to(path.resolve()) and evidence.is_file() and review.digest(review.read(evidence)) == blocker['sha']


def handle(project, event):
    if event.get('hook_event_name') != 'Stop' or not isinstance(event.get('session_id'), str):
        return {}
    runs = project / 'work/runs'
    session_id = event['session_id']
    binding_path = review.completion_path(runs, session_id)
    if not binding_path.is_file():
        return {}
    with review.locked(runs):
        binding = review.read(binding_path)
        if binding.get('sessionId') != session_id or binding.get('status') == 'complete':
            return {}
        path, state = review.load_run(runs, binding['run'])
        if state.get('completionSessionId') != session_id:
            return {'systemMessage': '整理已由其他会话接手；本会话不重复执行。'}
        complete = verified_complete(path, state)
        if complete and dt.datetime.fromisoformat(state['end']) >= dt.datetime.fromisoformat(binding['requiredEnd']):
            binding.update(status='complete', completedAt=review.stamp())
            review.save(binding_path, binding)
            return {}
        if not complete and evidenced_blocker(path, state, session_id):
            return {'systemMessage': '本轮有已登记的真实故障证据，保留原run及水位；报告实际缺口和下一步。'}
        if complete:
            active = review.read(runs / '.active.json')['run']
            other = review.load_run(runs, active)[1]
            if other['phase'] != 'complete' and other.get('completionSessionId') != session_id:
                return {'systemMessage': '另一个会话正在整理，保留当前范围，不覆盖或抢占。'}
        fingerprint = review.work_fingerprint(state)
        stalled = binding.get('stalledStops', 0) + 1 if binding.get('fingerprint') == fingerprint else 1
        binding.update(fingerprint=fingerprint, stalledStops=stalled)
        review.save(binding_path, binding)
    # start/record_failure acquire the ledger lock themselves.
    if complete:
        config = review.read(project / 'config.json')
        review.verify_identity(state, config)
        review.start(runs, config, pathlib.Path(config['dataDirectory']).expanduser(),
                     end=binding['requiredEnd'], session_id=session_id, expected_complete_run=state['id'])
        state = review.load_run(runs, review.read(binding_path)['run'])[1]
    elif stalled >= 3:
        reason = '自动续接连续3次无进展，停止重复执行；保留原run，需核对剩余步骤和工具可用性。'
        review.record_failure(runs, state['id'], reason, expected_session=session_id,
                              expected_fingerprint=fingerprint)
        return {'systemMessage': reason}
    return {'decision': 'block', 'reason': (
        f'自动整理尚未通过完成检查，继续原run {state["id"]}。下一步：{next_step(project, state)}。'
        '只做原范围；实际阅读后才ack，不能自动确认未读内容，不能以消息多或待核对为由结束。'
        '真实故障用本轮去敏错误JSON及fail --evidence登记；不重建编号、不推进未完成水位。')}


def main():
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=pathlib.Path, required=True)
    args = parser.parse_args()
    try:
        result = handle(args.project.resolve(), json.load(sys.stdin))
    except (OSError, ValueError, KeyError) as error:
        # Never report hook failure as review success or silently spin forever.
        result = {'systemMessage': f'自动整理结束检查无法读取凭据：{type(error).__name__}；保留原状态，请报告并排查。'}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()

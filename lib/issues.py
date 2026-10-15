"""나에게 할당된 이슈의 긴급도. Jev 분류는 선택 사항이고, 읽기 전용이다.

정렬 점수(rank)는 한 곳에서만 정한다 — Jev 가 확신 있게 답했으면 그 점수, 아니면
GitHub org 이슈 필드 Priority. 둘 다 없으면 Medium 과 같은 자리에 둔다.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import time

from lib import github, insight_api, selection, store

VERSION = 1
PER_TICK = 10                       # 틱당 Jev 호출 상한. 처음 한 번만 많고 그 뒤로는 바뀐 이슈만 돈다.
STATES = ('actionable', 'blocked', 'needsInfo', 'resolved', 'unknown')
PRIORITY_SCORE = {'Urgent': 2.0, 'High': 1.5, 'Medium': 1.0, 'Low': 0.5}


def path():
    return Path(store.STATE_DIR) / 'issue-selections.json'


def key(account, issue):
    return '%s:%s#%s:issue' % (account or 'global', issue['repo'], issue['number'])


def load():
    try:
        data = json.loads(path().read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(data):
    path().parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.issues-', dir=path().parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as file:
            json.dump(data, file, ensure_ascii=False)
        os.replace(name, path())
    finally:
        if os.path.exists(name):
            os.unlink(name)


def decision(raw):
    if raw.get('state') not in STATES:
        raise ValueError('invalid classification')
    for field, maximum in [('confidence', 1), ('urgency', 2)]:
        value = raw.get(field)
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not 0 <= value <= maximum:
            raise ValueError('invalid score')
    status = 'ready' if raw['confidence'] >= 0.8 and raw['state'] != 'unknown' else 'uncertain'
    return dict({k: raw[k] for k in ('state', 'urgency', 'confidence')}, status=status)


def watched(cfg, issue):
    entry = (cfg.get('repos') or {}).get(issue['repo'])
    return entry is not None and entry.get('enabled') is not False


def view(cache, registered, account, issue, cfg):
    """앱이 보는 분류 결과. 감시 저장소가 아니거나 Jev 가 꺼져 있으면 None."""
    if not watched(cfg, issue) or not registered.get('enabled'):
        return None
    row = cache.get(key(account, issue))
    if not row or row.get('registration') != registered.get('registration') or row.get('model') != registered.get('model'):
        return {'status': 'pending'}
    row = dict(row)
    if row.get('updatedAt') != issue.get('updatedAt') and row.get('status') in ('ready', 'uncertain'):
        row['status'] = 'stale'
    return row


def rank(issue, jev):
    """정렬에 쓰는 점수 하나와 그 출처. 앱은 이 값으로만 줄을 세운다."""
    if jev and jev.get('status') == 'ready':
        return {'score': float(jev['urgency']), 'source': 'jev'}
    priority = issue.get('priority')
    if priority in PRIORITY_SCORE:
        return {'score': PRIORITY_SCORE[priority], 'source': 'priority'}
    return {'score': PRIORITY_SCORE['Medium'], 'source': 'none'}


def refresh(targets, cfg, log):
    """(account, issue) 목록 중 감시 저장소의 이슈를 Jev 로 분류한다. 틱이 부른다."""
    registered = selection.registration()
    if not registered.get('enabled'):
        return
    targets = [(a, i) for a, i in targets if watched(cfg, i)]
    if not targets:
        return
    cache = load()
    unique = {key(a, i): (a, i) for a, i in targets}
    ordered = sorted(unique, key=lambda k: cache.get(k, {}).get('checkedAt', 0))
    for k in ordered[:PER_TICK]:
        account, issue = unique[k]
        old = cache.get(k, {})
        row = dict(old, repo=issue['repo'], number=issue['number'], updatedAt=issue.get('updatedAt'),
                   checkedAt=time.time(), model=registered['model'], registration=registered['registration'])
        cache[k] = row
        try:
            # 갱신 시각이 그대로면 대화도 그대로다 — 조회 자체를 아낀다.
            if old.get('updatedAt') == issue.get('updatedAt') and old.get('status') in ('ready', 'uncertain'):
                continue
            context = github.issue_context(issue['repo'], issue['number'], account=account)
            context['currentUser'] = account or github.whoami(account)
            fingerprint = hashlib.sha256(json.dumps([VERSION, registered['model'], registered['registration'], context],
                                                    sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if old.get('fingerprint') != fingerprint or old.get('status') == 'error':
                result = insight_api.call({'action': 'classify-issue', 'model': registered['model'],
                                           'registration': registered['registration'],
                                           'context': json.dumps(context, ensure_ascii=False)}, provider='jev')
                row.update(decision(result), fingerprint=fingerprint, error=None,
                           analyzedAt=time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                           inputTokens=result.get('inputTokens', 0), outputTokens=result.get('outputTokens', 0))
            if context.get('incomplete'):
                row['status'] = 'uncertain'
            row['incomplete'] = bool(context.get('incomplete'))
        except (github.GhError, insight_api.APIError, ValueError, OSError):
            row.update(status='error', error='Jev 이슈 분류를 갱신하지 못했습니다. 우선순위 필드로 정렬합니다.')
            log('Jev 이슈 분류 실패: %s#%s' % (issue['repo'], issue['number']))
    save({k: v for k, v in cache.items() if v.get('checkedAt', 0) > time.time() - 30 * 86400})

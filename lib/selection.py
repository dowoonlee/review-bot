"""Optional Jev classification. Read-only decisions never change review/post policy."""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

from lib import github, insights, insight_api, store

VERSION = 1


def registration():
    # No model registered: don't start the API helper or touch the keychain.
    try:
        result = subprocess.run(['/usr/bin/defaults', 'read', 'com.review-bot.app', 'jevModel'],
                                capture_output=True, text=True, timeout=3)
        model = result.stdout.strip()
        if result.returncode or not re.fullmatch(r'jev-[A-Za-z0-9.-]{1,80}', model):
            return {'enabled': False, 'model': '', 'registration': '', 'keyReady': False}
        raw = insight_api.call({'action': 'status'}, provider='jev')
        return {k: raw.get(k, default) for k, default in
                {'enabled': False, 'model': '', 'registration': '', 'keyReady': False}.items()}
    except (OSError, subprocess.SubprocessError, insight_api.APIError):
        return {'enabled': False, 'model': '', 'registration': '', 'keyReady': False}


def path():
    return Path(store.STATE_DIR) / 'request-selections.json'


def load():
    try:
        data = json.loads(path().read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(data):
    path().parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.selection-', dir=path().parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as file:
            json.dump(data, file, ensure_ascii=False)
        os.replace(name, path())
    finally:
        if os.path.exists(name): os.unlink(name)


def decision(raw):
    if raw.get('actor') not in ('self', 'other', 'none', 'unknown') or raw.get('action') not in ('fix', 'reply', 'review', 'wait', 'unknown'):
        raise ValueError('invalid classification')
    for field, maximum in [('confidence', 1), ('urgency', 2)]:
        value = raw.get(field)
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not 0 <= value <= maximum:
            raise ValueError('invalid score')
    coherent = ((raw['actor'] == 'self' and raw['action'] in ('fix', 'reply', 'review')) or
                (raw['actor'] in ('other', 'none') and raw['action'] == 'wait'))
    status = 'ready' if raw['confidence'] >= 0.8 and coherent else 'uncertain'
    return dict({k: raw[k] for k in ('actor', 'action', 'urgency', 'confidence')}, status=status)


def view(cache, registered, account, pr, direction, cfg=None):
    if cfg is not None:
        entry = (cfg.get('repos') or {}).get(pr['repo'])
        if entry is None or entry.get('enabled') is False: return None
    if not registered.get('enabled'):
        return None
    row = cache.get(insights.key(account, pr, direction))
    if not row or row.get('registration') != registered.get('registration') or row.get('model') != registered.get('model'):
        return {'status': 'pending'}
    row = dict(row)
    if row.get('updatedAt') != pr.get('updatedAt') and row.get('status') in ('ready', 'uncertain'):
        row['status'] = 'stale'
    return row


def refresh(targets, cfg, log):
    registered = registration()
    if not registered.get('enabled') or not targets: return
    cache = load()
    unique = {insights.key(a, p, d): (a, p, d) for a, p, d in targets}
    ordered = sorted(unique, key=lambda k: cache.get(k, {}).get('checkedAt', 0))
    claude = insights.load()
    for key in ordered[:3]:
        account, pr, direction = unique[key]
        old = cache.get(key, {})
        row = dict(old, repo=pr['repo'], number=pr['number'], updatedAt=pr.get('updatedAt'),
                   checkedAt=time.time(), model=registered['model'], registration=registered['registration'])
        cache[key] = row
        try:
            context = github.request_context(pr['repo'], pr['number'], account=account)
            context.update(direction=direction, currentUser=account or github.whoami(account))
            explanation = insights.view(claude, cfg, account, pr, direction)
            if explanation.get('status') == 'ready':
                context['analysis'] = {k: explanation.get(k) for k in ('summary', 'items')}
            fingerprint = hashlib.sha256(json.dumps([VERSION, registered['model'], registered['registration'], context], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if old.get('fingerprint') != fingerprint or old.get('status') == 'error':
                result = insight_api.call({'action': 'classify', 'model': registered['model'],
                    'registration': registered['registration'], 'context': json.dumps(context, ensure_ascii=False)}, provider='jev')
                row.update(decision(result), fingerprint=fingerprint, error=None,
                           analyzedAt=time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                           inputTokens=result.get('inputTokens', 0), outputTokens=result.get('outputTokens', 0))
            if context.get('incomplete'):
                row['status'] = 'uncertain'
            row['incomplete'] = bool(context.get('incomplete'))
        except (github.GhError, insight_api.APIError, ValueError, OSError):
            row.update(status='error', error='Jev 분류를 갱신하지 못했습니다. 기존 판단을 유지합니다.')
            log('Jev 분류 갱신 실패: %s#%s' % (pr['repo'], pr['number']))
    save({k: v for k, v in cache.items() if v.get('checkedAt', 0) > time.time() - 30 * 86400})

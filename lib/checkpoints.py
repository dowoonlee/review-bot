"""Durable same-revision review evidence and bounded CI rejudging."""
import hashlib
import json
from pathlib import Path
import time

from lib import store, config, agents, validation


def _path(repo, number):
    key = hashlib.sha256(('%s#%s' % (repo, number)).encode()).hexdigest()
    return Path(store.STATE_DIR, 'checkpoints', key + '.json')


def digest(value, files=()):
    root = Path(__file__).resolve().parents[1]
    value_hash = hashlib.sha256(json.dumps(value, sort_keys=True).encode())
    for name in files:
        value_hash.update((root / name).read_bytes())
    return value_hash.hexdigest()


def identity(meta, cfg, account):
    review_cfg = {k: v for k, v in cfg.get('review', {}).items() if k != 'runtime'}
    definitions = {}
    for name in review_cfg.get('agents', []):
        try:
            definitions[name] = agents.role(name)
        except (agents.AgentError, OSError):
            definitions[name] = None
    review_key = digest({'config': review_cfg, 'roles': definitions, 'repomind': cfg.get('repomind')},
                        ('lib/reviewer.py', 'lib/batches.py', 'lib/agents.py', 'review-bot',
                         'prompts/review.md', 'prompts/verify.md'))
    entry = (cfg.get('repos') or {}).get(meta.get('_repo')) or {}
    runtime_cfg = config._merge(cfg.get('review', {}).get('runtime') or {}, entry.get('runtime') or {})
    runtime_cfg.pop('minFreeDiskGiB', None)  # Admission capacity does not change completed evidence.
    runtime_key = digest({'config': runtime_cfg,
                          'scope': {k: review_cfg.get(k) for k in ('includeExts', 'excludeGlobs')}},
                         ('lib/runtime.py', 'lib/validation.py', 'lib/reviewer.py', 'review-bot', 'prompts/runtime.md'))
    post_cfg = {k: v for k, v in cfg.get('post', {}).items()
                if k not in ('dryRun', 'polish', 'note', 'summaryComment')}
    decision_key = digest({'review': review_key, 'runtime': runtime_key, 'post': post_cfg},
                          ('lib/policy.py', 'prompts/verdict.md'))
    return {'head': meta.get('headRefOid'), 'base': meta.get('baseRefOid'),
            'account': account, 'review': review_key, 'runtime': runtime_key, 'decision': decision_key}


def load(repo, number, expected=None):
    try:
        value = json.loads(_path(repo, number).read_text())
        if expected is not None:
            original = value.get('identity') or {}
            if any(original.get(k) != expected.get(k) for k in ('head', 'base', 'account')):
                return None
            reuse = {k: bool(expected.get(k)) and original.get(k) == expected[k]
                     for k in ('review', 'runtime', 'decision')}
            if not reuse['review'] and not reuse['runtime']:
                return None  # Legacy checkpoints have no separable evidence identity.
            value['_reuse'] = reuse
        return value
    except (OSError, ValueError):
        return None


def obligations(repo, number, expected):
    """Required checks for this exact revision, even when no evidence is reusable.

    Any bot change invalidates earlier outcomes (and a change to review-bot itself
    invalidates both review and runtime, so load() returns None), but not what this
    PR must prove.
    """
    try:
        value = json.loads(_path(repo, number).read_text())
    except (OSError, ValueError):
        return None
    original = value.get('identity') or {}
    if any(original.get(k) != expected.get(k) for k in ('head', 'base', 'account')):
        return None
    return validation.carry((value.get('payload') or {}).get('runtime'))


def save(repo, number, value):
    path = _path(repo, number)
    path.parent.mkdir(parents=True, exist_ok=True)
    value = dict(value, repo=repo, number=number, updated=time.time())
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False))
    temporary.replace(path)


def watching(cfg):
    result = []
    for path in Path(store.STATE_DIR, 'checkpoints').glob('*.json'):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        age = time.time() - data.get('created', 0)
        if age > 30 * 86400:
            path.unlink(missing_ok=True)
        elif data.get('watchCI') and age < 7 * 86400 and data['repo'] in cfg.get('repos', {}):
            result.append({'repo': data['repo'], 'number': data['number'],
                           'title': data.get('title', ''), '_rejudge': True})
    return result


def stop(repo, number):
    value = load(repo, number)
    if value:
        value['watchCI'] = False
        save(repo, number, value)


def unchanged_wait(value, ci, now=None):
    now = time.time() if now is None else now
    return (all(value.get('_reuse', {'decision': True}).values()) and value.get('ci') == ci and
            (not value.get('waiting') or now - value.get('waitingSince', now) < 86400))


def judgment_key(identity, payload, candidates, ci):
    meaningful = ('path', 'line', 'title', 'body', 'severity', 'agent', '_scope', '_why', '_verify')
    return digest({'policy': identity['decision'], 'ci': ci,
                   'evidence': {k: payload.get(k) for k in ('summary', 'errors', 'coverage', 'runtime')},
                   'findings': [{k: f.get(k) for k in meaningful} for f in candidates]})


def saved_judgment(checkpoint, key):
    saved = (checkpoint or {}).get('judgment') or {}
    if saved.get('key') != key:
        return None
    verdict = json.loads(json.dumps(saved['verdict']))
    for field in ('placements', 'dispositions'):
        verdict[field] = {int(k): v for k, v in (verdict.get(field) or {}).items()}
    verdict['cost_usd'] = 0
    return verdict

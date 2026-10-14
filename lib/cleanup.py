"""Remove one PR's local artifacts under the bot lock, preserving other PRs."""
import json
import os
from pathlib import Path
import re
import shutil

from lib import checkpoints, store, resources, home_tasks


def matches(row, repo, number):
    return isinstance(row, dict) and row.get('repo') == repo and str(row.get('number')) == str(number)


def read_json(path):
    try:
        if path.is_symlink():
            return None
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def clean_pr(repo, number, reset=False, preview=False):
    """Caller holds store.acquire_lock(). No GitHub writes or shared-resource cleanup."""
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9_.-]+', repo) or int(number) <= 0:
        raise ValueError('올바른 owner/repo와 PR 번호가 필요합니다')
    declared_root = Path(store.STATE_DIR).absolute()
    root = declared_root.resolve()
    def canonical(path):
        path = Path(path)
        return root / path.relative_to(declared_root) if path.is_relative_to(declared_root) else path
    number = int(number)
    selected, rewrites, counts = [], {}, {}
    for name in ('runs.jsonl', 'verify.jsonl'):
        path = root / name
        kept, removed = [], 0
        if path.exists():
            for line in path.read_text().splitlines(keepends=True):
                try:
                    row = json.loads(line)
                except ValueError:
                    kept.append(line)
                    continue
                if matches(row, repo, number):
                    selected.append(row)
                    removed += 1
                else:
                    kept.append(line)
            if removed:
                rewrites[path] = ''.join(kept)
        counts[name] = removed
    for cache_name in ('request-insights.json', 'request-selections.json'):
        cache_path = root / cache_name
        cache_data = read_json(cache_path)
        if cache_data is not None:
            kept = {k: v for k, v in cache_data.items() if not matches(v, repo, number)}
            counts[cache_name] = len(cache_data) - len(kept)
            if len(kept) != len(cache_data):
                rewrites[cache_path] = json.dumps(kept, ensure_ascii=False)
    task_data = read_json(root / 'home-tasks.json') or {}
    counts['home-tasks.json'] = sum(matches(row, repo, number) for row in (task_data.get('tasks') or {}).values())
    checkpoint_path = canonical(checkpoints._path(repo, number))
    checkpoint = read_json(checkpoint_path) or {}
    selected.append(checkpoint)
    selected.append(checkpoint.get('payload') or {})
    drafts = canonical(store.DRAFTS)
    draft_path = drafts / (repo.replace('/', '__') + '-%s.json' % number)
    draft = read_json(draft_path)
    paths, skipped = set(), []
    if matches(draft, repo, number):
        paths.update((draft_path, draft_path.with_suffix('.md')))
        selected.append(draft)
    if checkpoint_path.exists():
        paths.add(checkpoint_path)
    archive = drafts / 'archived'
    for path in archive.glob('*.json'):
        if matches(read_json(path), repo, number):
            paths.add(path)

    scope = root / 'evidence' / re.sub(r'[^a-zA-Z0-9_-]', '_', repo) / str(number)
    references = set()
    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ('directory', 'log', 'reportArtifact') and isinstance(item, str):
                    candidate = canonical(item)
                    if key != 'directory': candidate = candidate.parent
                    references.add(candidate)
                elif key == 'screenshotArtifacts' and isinstance(item, list):
                    references.update(canonical(p).parent for p in item if isinstance(p, str))
                else:
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    visit(selected)

    def safe(path):
        return (path.is_relative_to(root) and '..' not in path.parts and
                path.resolve().is_relative_to(root) and
                not any(p.is_symlink() for p in (path, *path.parents) if p.is_relative_to(root)))

    if scope.exists() and safe(scope):
        for head in scope.iterdir():
            if not head.is_dir() or not safe(head): continue
            for run in head.iterdir():
                if not run.is_dir() or not safe(run):
                    skipped.append(str(run)); continue
                owner = read_json(run / 'owner.json') or read_json(run / 'result.json')
                if owner and owner.get('instance') and (owner['instance'] != resources.instance_id() or resources.alive(owner)):
                    skipped.append(str(run)); continue
                owned = matches(owner, repo, number) if owner and 'repo' in owner else run in references
                if owned:
                    paths.add(run)
                else:
                    skipped.append(str(run))
    existing = sorted((p for p in paths if p.exists() and safe(p)), key=str)
    report = {'repo': repo, 'number': number, 'records': counts,
              'artifacts': [str(p) for p in existing], 'unowned': skipped, 'preview': preview}
    if preview:
        return report

    # Dropping a draft must not accidentally queue a fresh automatic review.
    state = store.load()
    key = store.key(repo, number)
    known = [r for r in selected if matches(r, repo, number) and r.get('sha')]
    record = state.get(key) or (draft if matches(draft, repo, number) else None) or max(known, key=lambda r: r.get('at', ''), default=None)
    if record:
        state[key] = {k: record[k] for k in ('sha', 'at', 'requestedAt', 'posted') if k in record}
        state[key].update(discarded=True, pending=False, event='DISCARDED')
        store.save(state)
    checkpoints.stop(repo, number)
    home_tasks.forget(repo, number)
    errors = []
    for path in existing:
        try:
            if path.is_dir(): shutil.rmtree(path)
            else: path.unlink()
        except OSError as exc:
            errors.append(str(exc))
    for path, content in rewrites.items():
        temporary = path.with_suffix(path.suffix + '.tmp')
        temporary.write_text(content)
        if path.name in ('request-insights.json', 'request-selections.json'):
            os.chmod(temporary, 0o600)
        temporary.replace(path)
    if reset and not errors:
        state.pop(key, None)
        state.pop(store.skip_key(repo, number), None)
        store.save(state)
    if scope.exists() and safe(scope):
        for head in scope.iterdir():
            if head.is_dir() and safe(head):
                try: head.rmdir()
                except OSError: pass
        try: scope.rmdir()
        except OSError: pass
    report['errors'] = errors
    return report

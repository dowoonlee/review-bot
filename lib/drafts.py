"""Reconcile pending review drafts against confirmed GitHub PR state."""
import json
import os
import time

from lib import config, github, store


def reconcile(cfg, locked=False):
    pending = [d for d in store.pending_drafts() if config.repo_entry(cfg, d['repo'])]
    if not pending:
        return []
    if not locked and not store.acquire_lock():
        return []
    errors = []
    try:
        for draft in pending:
            repo, number = draft['repo'], draft['number']
            entry = config.repo_entry(cfg, repo)
            account = entry.get('account') or cfg.get('account')
            try:
                meta = github._gh_json(['pr', 'view', str(number), '--repo', repo,
                                        '--json', 'state'], account=account)
                if meta.get('state') not in ('MERGED', 'CLOSED'):
                    continue
                path = store.draft_payload_path(repo, number)
                # A newer draft must never be archived using an older snapshot.
                with open(path, encoding='utf-8') as source:
                    if json.load(source) != draft:
                        continue
                archive = os.path.join(store.DRAFTS, 'archived')
                os.makedirs(archive, exist_ok=True)
                name = '%s-%s-%s.json' % (os.path.basename(path)[:-5],
                                         meta['state'].lower(), time.time_ns())
                os.rename(path, os.path.join(archive, name))
            except FileNotFoundError:
                continue
            except (github.GhError, OSError, ValueError) as exc:
                errors.append('초안 상태 확인 실패 (%s#%s): %s' % (repo, number, str(exc)[:200]))
    finally:
        if not locked:
            store.release_lock()
    return errors

"""Reconcile pending review drafts against confirmed GitHub PR state."""
import json
import os
import time

from lib import config, github, policy, store

EVENTS = ('APPROVE', 'COMMENT', 'REQUEST_CHANGES')
LABEL = {'APPROVE': '승인', 'COMMENT': '코멘트', 'REQUEST_CHANGES': '변경 요청'}


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
                # 게시가 이 초안을 쓰고 있으면 건드리지 않는다 — 다음에 다시 본다.
                with store.pr_lock(repo, number) as owned:
                    if not owned:
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


def set_event(repo, number, event):
    """Rewrite a pending draft's review event before it is published.

    The judge's verdict is kept under ``override`` so the user can return to it,
    and the decision chain records HUMAN_OVERRIDE so the run history says who
    chose the final event. The body and inline comments stay as they were made;
    ``at`` is untouched so the app does not announce the draft again.
    """
    if event not in EVENTS:
        raise ValueError('지원하지 않는 이벤트: %s (%s)' % (event, ', '.join(EVENTS)))
    path = store.draft_payload_path(repo, number)
    with open(path, encoding='utf-8') as source:
        draft = json.load(source)
    current = draft.get('event') or 'COMMENT'
    if event == current:
        return draft
    override = draft.get('override')
    if override and override.get('from') == event:
        draft.update(event=event, reason=override.get('reason'), decision=override.get('decision'))
        draft.pop('override', None)
        if draft.get('intent') != event:
            draft.pop('intent', None)   # 봇의 결론으로 돌아갔다 — 다음 재리뷰에 넘길 방향이 없다
    else:
        if not override:
            override = {'from': current, 'reason': draft.get('reason'),
                        'decision': draft.get('decision'),
                        'at': time.strftime('%Y-%m-%dT%H:%M:%S')}
            draft['override'] = override
        message = '사용자가 판정(%s) 대신 %s(으)로 게시하기로 했습니다.' % (
            LABEL[override['from']] if override['from'] in LABEL else override['from'], LABEL[event])
        previous = draft.get('decision') or {
            'judgeEvent': current, 'finalEvent': current,
            'summary': draft.get('reason') or '', 'reasons': []}
        draft['decision'] = policy.revise_decision(previous, event, 'HUMAN_OVERRIDE', message, 'user')
        draft['reason'] = message
        draft['event'] = event
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as target:
        json.dump(draft, target, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    _rewrite_report_header(store.draft_path(repo, number), event)
    return draft


def user_intent(repo, number):
    """대기 중인 초안에 사람이 고른 게시 방식이 있으면 그 이벤트, 없으면 None.

    봇의 판정을 그대로 둔 초안이면 따를 사람의 방향이 없다. 재리뷰가 이어받은
    방향(`intent`)도 사람이 고른 것이므로 다음 재리뷰에 다시 넘긴다.
    """
    try:
        with open(store.draft_payload_path(repo, number), encoding='utf-8') as source:
            draft = json.load(source)
    except (OSError, ValueError):
        return None
    if not (draft.get('override') or draft.get('intent')):
        return None
    event = draft.get('event')
    return event if event in EVENTS else None


def _rewrite_report_header(path, event):
    """Keep the human-readable .md in step with the event that will be posted."""
    try:
        with open(path, encoding='utf-8') as source:
            lines = source.readlines()
    except OSError:
        return
    if not lines or not lines[0].startswith('<!-- event:'):
        return
    lines[0] = '<!-- event: %s -->\n' % event
    with open(path, 'w', encoding='utf-8') as target:
        target.writelines(lines)

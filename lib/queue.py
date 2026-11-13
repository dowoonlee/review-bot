"""Reviews a person asked for while the bot was busy.

The tick still asks GitHub for the open review requests every time; that list needs no
storage. What had nowhere to go was a person's own request (`review-bot review`, the
app's 재리뷰) made while another review held the lock: it was refused. Those wait here,
in order, and whoever holds the lock takes them before the tick's own targets.

One entry per PR. Asking again updates its options and keeps its place.
"""
from contextlib import contextmanager
import fcntl
import json
import os
import time

from lib import store

OPTIONS = ('rejudge', 'agents', 'note', 'forceSize')


def path():
    return os.path.join(store.STATE_DIR, 'queue.json')


def _read():
    try:
        with open(path(), encoding='utf-8') as stream:
            data = json.load(stream)
    except (OSError, ValueError):
        return []
    return [item for item in data if isinstance(item, dict) and item.get('repo') and item.get('number')] \
        if isinstance(data, list) else []


@contextmanager
def _locked():
    """Read-modify-write under one lock; the list the caller mutates is what gets saved."""
    os.makedirs(store.STATE_DIR, exist_ok=True)
    with open(path() + '.lock', 'a+') as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        items = _read()
        before = json.dumps(items, sort_keys=True)
        yield items
        if json.dumps(items, sort_keys=True) != before:
            temporary = path() + '.tmp'
            with open(temporary, 'w', encoding='utf-8') as stream:
                json.dump(items, stream, ensure_ascii=False, indent=2)
            os.replace(temporary, path())


def _index(items, repo, number):
    return next((i for i, item in enumerate(items)
                 if item['repo'] == repo and item['number'] == number), None)


def items():
    return _read()


def pending():
    return bool(_read())


def add(repo, number, options=None, front=False):
    """Queue a PR and return its 1-based position."""
    options = {k: v for k, v in (options or {}).items() if k in OPTIONS and v}
    with _locked() as queued:
        index = _index(queued, repo, number)
        if index is None:
            entry = {'repo': repo, 'number': number, 'options': options,
                     'enqueuedAt': time.strftime('%Y-%m-%dT%H:%M:%S')}
        else:
            entry = dict(queued.pop(index), options=options)
        if front:
            queued.insert(0, entry)
        elif index is None:
            queued.append(entry)
        else:
            queued.insert(index, entry)
        return _index(queued, repo, number) + 1


def first(skip=()):
    """The next request, leaving it queued: it is removed only once its PR is held."""
    return next((item for item in _read() if (item['repo'], item['number']) not in skip), None)


def remove(repo, number):
    with _locked() as queued:
        index = _index(queued, repo, number)
        if index is None:
            return False
        queued.pop(index)
        return True


def to_front(repo, number):
    with _locked() as queued:
        index = _index(queued, repo, number)
        if index is None:
            return False
        queued.insert(0, queued.pop(index))
        return True


def clear():
    with _locked() as queued:
        count = len(queued)
        del queued[:]
        return count

"""Ownership and recovery for disposable resources, scoped to this host/workspace."""
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import time

from lib import store


def instance_id():
    return hashlib.sha256((socket.gethostname() + '\0' + str(Path(store.STATE_DIR).resolve())).encode()).hexdigest()[:24]


def process_start(pid):
    try:
        result = subprocess.run(['ps', '-p', str(pid), '-o', 'lstart='], capture_output=True,
                                text=True, timeout=5, env={**os.environ, 'LC_ALL': 'C', 'TZ': 'UTC'})
        if result.returncode or not result.stdout.strip():
            return None
        return hashlib.sha256(' '.join(result.stdout.split()).encode()).hexdigest()
    except (OSError, subprocess.SubprocessError):
        return None


def owner():
    return {'instance': instance_id(), 'pid': os.getpid(), 'processStarted': process_start(os.getpid()),
            'created': time.time()}


def alive(record):
    """Unknown ownership/liveness is protected; PID reuse is not an active owner."""
    try:
        pid = int(record['pid'])
        if pid <= 0 or not record.get('processStarted'):
            return True
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (KeyError, ValueError, TypeError, OSError, OverflowError):
        return True
    current = process_start(pid)
    return current is None or current == record['processStarted']


def owner_key(record):
    return '%s:%s' % (record.get('pid'), record.get('processStarted'))


def read_owner(path):
    try:
        if Path(path).is_symlink(): return {}
        value = json.loads(Path(path).read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def sweep_containers():
    """Caller holds the bot lock. Never prune images, networks or unrelated volumes."""
    result = {'removed': [], 'kept': [], 'errors': [], 'blocked': []}
    try:
        query = subprocess.run(['docker', 'ps', '-aq', '--filter', 'label=review-bot.runtime=true'],
                               capture_output=True, text=True, timeout=15)
        if query.returncode:
            raise RuntimeError('Docker 엔진에 연결할 수 없어 컨테이너 회수를 보류합니다.')
        for identifier in query.stdout.split():
            inspection = subprocess.run(['docker', 'inspect', identifier], capture_output=True, text=True, timeout=15)
            if inspection.returncode:
                if 'No such' in inspection.stderr: continue
                raise RuntimeError('컨테이너 소유 정보 조회 실패: ' + identifier)
            data = json.loads(inspection.stdout)[0]
            labels = data.get('Config', {}).get('Labels') or {}
            if labels.get('review-bot.instance') != instance_id():
                continue
            record = {'instance': labels.get('review-bot.instance'), 'pid': labels.get('review-bot.owner-pid'),
                      'processStarted': labels.get('review-bot.owner-start')}
            name = data.get('Name', '').lstrip('/')
            if not name.startswith('review-bot-') or alive(record):
                result['kept'].append(name)
                result['blocked'].append(owner_key(record))
                continue
            removal = subprocess.run(['docker', 'rm', '-f', '-v', identifier],
                                     capture_output=True, text=True, timeout=30)
            if removal.returncode and 'No such container' not in removal.stderr:
                result['errors'].append('컨테이너 회수 실패: ' + name)
                result['blocked'].append(owner_key(record))
            else:
                result['removed'].append(name)
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError, RuntimeError) as exc:
        result['errors'].append(str(exc)[:250])
        result['blocked'].append('*')  # Do not remove evidence while Docker ownership is unknown.
    return result

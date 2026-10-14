"""Cheap execution prerequisites, checked before paying for a generated plan."""
import json
from pathlib import Path
import re
import shutil
import subprocess


def memory_bytes(value):
    match = re.fullmatch(r'(\d+(?:\.\d+)?)\s*([kmgt]?)(?:i?b)?', str(value).strip(), re.I)
    if not match:
        return None
    return int(float(match[1]) * 1024 ** (' kmgt'.index(match[2].lower()) if match[2] else 0))


def check(cfg, directory):
    if not cfg.get('enabled', True):
        return {'ok': True, 'checks': [], 'reason': ''}
    checks = []
    def add(code, ok, message, **details):
        checks.append(dict(code=code, ok=bool(ok), message=message, **details))
    location = Path(directory).resolve()
    while not location.exists() and location != location.parent:
        location = location.parent
    try:
        free = shutil.disk_usage(location).free
        minimum = int(float(cfg.get('minFreeDiskGiB', 3)) * 1024 ** 3)
        add('DISK_SPACE', free >= minimum, '호스트 작업 디스크 %.1f GiB 여유 (최소 %.1f GiB)' %
            (free / 1024 ** 3, minimum / 1024 ** 3), availableBytes=free, requiredBytes=minimum)
    except (OSError, ValueError, TypeError) as exc:
        add('DISK_SPACE', False, '작업 디스크 여유 확인 실패: ' + str(exc)[:150])
    if not shutil.which('docker'):
        add('DOCKER_AVAILABLE', False, 'Docker CLI가 설치되어 있지 않습니다.')
    else:
        try:
            # Only public numeric capacity fields; do not dump proxy/registry configuration.
            proc = subprocess.run(['docker', 'info', '--format', '{"memory":{{.MemTotal}},"cpus":{{.NCPU}}}'],
                                  capture_output=True, text=True, timeout=10)
            if proc.returncode:
                add('DOCKER_AVAILABLE', False, 'Docker 엔진에 연결할 수 없습니다. Docker를 기동한 뒤 다시 확인합니다.')
            else:
                info = json.loads(proc.stdout)
                add('DOCKER_AVAILABLE', True, 'Docker 엔진 연결 정상')
                required = memory_bytes(cfg.get('memory', '6g'))
                available = int(info['memory'])
                add('DOCKER_MEMORY', required is not None and available >= required,
                    'Docker 메모리 한도 %.1f GiB / 컨테이너 요청 %s' % (available / 1024 ** 3, cfg.get('memory', '6g')),
                    availableBytes=available, requiredBytes=required)
                cpus = float(cfg.get('cpus', 4))
                add('DOCKER_CPUS', 0 < cpus <= float(info['cpus']),
                    'Docker CPU %s개 / 컨테이너 요청 %s개' % (info['cpus'], cpus))
        except (OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError) as exc:
            add('DOCKER_AVAILABLE', False, 'Docker 상태 확인 실패: ' + str(exc)[:150])
    failures = [c['message'] for c in checks if not c['ok']]
    return {'ok': not failures, 'checks': checks, 'reason': ' · '.join(failures)}

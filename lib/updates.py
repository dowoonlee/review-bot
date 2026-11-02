"""User-requested, staged release updates. Never modifies configuration or review data."""
import fcntl
import hashlib
import json
import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from contextlib import contextmanager
from pathlib import Path

from lib import paths, store

REPOSITORY = 'dowoonlee/review-bot'
ASSET = 'review-bot-macos-universal.zip'
PROTOCOL = 1
STATE_SCHEMA = 1


class UpdateError(Exception):
    pass


def _file(name):
    return Path(store.STATE_DIR, name)


def read(name, default=None):
    try:
        return json.loads(_file(name).read_text())
    except FileNotFoundError:
        return {} if default is None else default


def write(name, data):
    target = _file(name)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=target.parent, delete=False) as out:
        json.dump(data, out, ensure_ascii=False, indent=2)
        temp = out.name
    os.replace(temp, target)


@contextmanager
def guard(name='update.guard', blocking=True):
    _file(name).parent.mkdir(parents=True, exist_ok=True)
    with _file(name).open('a+') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            raise UpdateError('업데이트 작업이 이미 실행 중입니다.') from None
        yield


def command(args, **kwargs):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=180, **kwargs)
    except (OSError, subprocess.SubprocessError) as exc:
        raise UpdateError('업데이트 명령을 실행하지 못했습니다: ' + type(exc).__name__) from None
    if result.returncode:
        # Never surface credentials or URLs from git/gh diagnostics.
        if args[:2] == ['gh', 'api'] and 'HTTP 404' in result.stderr:
            raise UpdateError('릴리스를 찾지 못했습니다. 공개된 정식 릴리스가 있는지와 gh 계정의 저장소 접근 권한을 확인하세요.')
        raise UpdateError('%s 실행 실패 (종료 코드 %s). 로그인·네트워크·설치 권한을 확인하세요.'
                          % (Path(args[0]).name, result.returncode))
    return result.stdout


def number(value):
    match = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)', value or '')
    if not match:
        raise UpdateError('정식 버전은 v1.2.3 형식이어야 합니다.')
    return tuple(map(int, match.groups()))


def active_root():
    pointer = paths.HOME / '.review-bot-current'
    if not pointer.is_symlink():
        if pointer.exists():
            raise UpdateError('업데이트 실행 경로가 심볼릭 링크가 아닙니다. 기존 파일을 덮어쓰지 않습니다.')
        return paths.HOME
    root = pointer.resolve(strict=True)
    if root.parent != _file('releases').resolve() or not (root / 'review-bot').is_file():
        raise UpdateError('업데이트 실행 경로가 올바르지 않습니다.')
    return root


def status():
    saved = read('update.json')
    saved['installedVersion'] = paths.version(active_root())
    saved['runningVersion'] = paths.version()
    saved['pending'] = bool(read('update-request.json'))
    return saved


def check(tag=None):
    if tag:
        number(tag)
    endpoint = 'tags/' + tag if tag else 'latest'
    try:
        release = json.loads(command(['gh', 'api', 'repos/%s/releases/%s' % (REPOSITORY, endpoint)]))
        version = release['tag_name']
        number(version)
        if release.get('draft') or release.get('prerelease'):
            raise UpdateError('공개된 정식 릴리스만 설치할 수 있습니다.')
        asset = next((a for a in release.get('assets', []) if a['name'] == ASSET), None)
        if not asset or not re.fullmatch(r'sha256:[0-9a-f]{64}', asset.get('digest') or ''):
            raise UpdateError('검증 가능한 macOS 배포 파일이 없는 릴리스입니다.')
        if asset.get('size', 0) > 256 * 1024 * 1024:
            raise UpdateError('릴리스 다운로드 크기가 제한을 초과했습니다.')
        latest = {'version': version, 'assetID': int(asset['id']), 'digest': asset['digest'],
                  'url': release.get('html_url', ''), 'notes': release.get('body') or ''}
        current = paths.version(active_root())
        available = current == 'unversioned' or number(version) > number(current)
        with guard():
            saved = read('update.json')
            saved.update(latest=latest, available=available, checkedAt=time.time(), checkError=None)
            write('update.json', saved)
        return status()
    except (UpdateError, ValueError, KeyError, TypeError) as exc:
        with guard():
            saved = read('update.json')
            saved.update(checkError=str(exc), checkedAt=time.time())
            write('update.json', saved)
        raise


def ensure_clean(root):
    root = Path(root)
    if (root / '.git').exists():
        dirty = command(['git', '-C', str(root), 'status', '--porcelain', '--untracked-files=normal'])
        if dirty:
            raise UpdateError('로컬 수정이 있어 업데이트하지 않습니다. 변경을 커밋하거나 별도로 보관한 뒤 다시 시도하세요.')
    elif (root / 'release.json').is_file():
        verify_tree(root)
    else:
        raise UpdateError('설치 파일의 무결성을 확인할 수 없습니다. Git 설치 또는 정식 릴리스가 필요합니다.')


def identity(root):
    root = Path(root)
    if (root / '.git').exists():
        return 'git:' + command(['git', '-C', str(root), 'rev-parse', 'HEAD']).strip()
    if (root / 'release.json').exists():
        return 'release:' + hashlib.sha256((root / 'release.json').read_bytes()).hexdigest()
    return 'version:' + paths.version(root)


def rollback_root(previous):
    root = Path(previous['root'])
    ensure_clean(root)
    if not previous.get('identity') or identity(root) != previous['identity']:
        raise UpdateError('이전 설치본이 이후 변경되어 안전하게 되돌릴 수 없습니다. 해당 버전을 별도로 복구하세요.')
    return root


def verify_tree(root):
    root = Path(root)
    manifest = json.loads((root / 'release.json').read_text())
    if manifest.get('protocol') != PROTOCOL or manifest.get('stateSchema') != STATE_SCHEMA:
        raise UpdateError('이 릴리스는 별도 데이터 마이그레이션이 필요합니다. 자동 적용하지 않습니다.')
    number(manifest.get('version'))
    files = manifest.get('files') or {}
    if not {'review-bot', 'VERSION', 'lib/updates.py', 'app/ReviewBot.app/Contents/Info.plist'}.issubset(files):
        raise UpdateError('릴리스 필수 파일이 없습니다.')
    for name, digest in files.items():
        path = root / name
        if path.is_symlink() or root.resolve() not in path.resolve().parents or not path.is_file():
            raise UpdateError('릴리스 파일 경로가 올바르지 않습니다.')
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise UpdateError('릴리스 파일이 변경되었거나 손상됐습니다: ' + name)
    for path in root.rglob('*'):
        relative = path.relative_to(root)
        if '__pycache__' in relative.parts:
            continue
        if path.is_symlink() or (path.is_file() and str(relative) not in files and str(relative) != 'release.json'):
            raise UpdateError('릴리스에 등록되지 않은 파일이 있습니다: ' + str(relative))
    if paths.version(root) != manifest['version'].lstrip('v'):
        raise UpdateError('릴리스 버전과 코드 버전이 일치하지 않습니다.')
    with (root / 'app/ReviewBot.app/Contents/Info.plist').open('rb') as src:
        info = plistlib.load(src)
    if info.get('CFBundleIdentifier') != 'com.review-bot.app':
        raise UpdateError('릴리스의 앱 식별자가 올바르지 않습니다.')
    if info.get('CFBundleShortVersionString') != paths.version(root):
        raise UpdateError('앱과 봇의 버전이 일치하지 않습니다.')
    return manifest


def check_app(app, version):
    if sys.platform != 'darwin':
        return
    command(['codesign', '--verify', '--deep', '--strict', str(app)])
    if command([str(Path(app, 'Contents/MacOS/ReviewBot')), '--version']).strip() != version:
        raise UpdateError('새 앱의 시작 검사 또는 버전 검증에 실패했습니다.')


def extract(archive, destination):
    """No links, traversal, duplicate members, or persistent data in a release."""
    with zipfile.ZipFile(archive) as bundle:
        if sum(i.file_size for i in bundle.infolist()) > 512 * 1024 * 1024:
            raise UpdateError('릴리스 압축 해제 크기가 제한을 초과했습니다.')
        seen = set()
        for item in bundle.infolist():
            name = Path(item.filename)
            mode = item.external_attr >> 16
            if (name.is_absolute() or '..' in name.parts or not name.parts or
                    name.parts[0] in ('state', 'config.json', '.git') or
                    stat.S_ISLNK(mode) or str(name) in seen or '\\' in item.filename):
                raise UpdateError('안전하지 않은 릴리스 파일 경로입니다.')
            seen.add(str(name))
            target = Path(destination, name)
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(item) as src, target.open('wb') as out:
                    shutil.copyfileobj(src, out)
                target.chmod(0o755 if mode & 0o111 else 0o644)


def prepare(release):
    releases = _file('releases')
    releases.mkdir(parents=True, exist_ok=True)
    destination = releases / (release['version'] + '-' + uuid.uuid4().hex[:12])
    with tempfile.TemporaryDirectory(dir=releases, prefix='.download-') as temp:
        archive = Path(temp, 'release.zip')
        with archive.open('wb') as out:
            try:
                result = subprocess.run(['gh', 'api', 'repos/%s/releases/assets/%s' %
                                         (REPOSITORY, release['assetID']),
                                         '-H', 'Accept: application/octet-stream'],
                                        stdout=out, stderr=subprocess.PIPE, timeout=600)
            except (OSError, subprocess.SubprocessError):
                raise UpdateError('릴리스 다운로드에 실패했습니다.') from None
        if result.returncode or 'sha256:' + hashlib.sha256(archive.read_bytes()).hexdigest() != release['digest']:
            raise UpdateError('릴리스 다운로드 또는 SHA256 검증에 실패했습니다.')
        tree = Path(temp, 'tree')
        tree.mkdir()
        extract(archive, tree)
        manifest = verify_tree(tree)
        if number(manifest['version']) != number(release['version']):
            raise UpdateError('선택한 릴리스와 배포 파일의 버전이 다릅니다.')
        # Check imports without consulting config, starting reviews or touching user data.
        env = dict(os.environ, REVIEW_BOT_NO_REDIRECT='1', REVIEW_BOT_HOME=str(paths.HOME))
        command([sys.executable, str(tree / 'review-bot'), 'update', '--self-check'], env=env)
        check_app(tree / 'app/ReviewBot.app', paths.version(tree))
        os.rename(tree, destination)
    return destination


def pending():
    return bool(read('update-request.json'))


def _progress(phase, message, job_id=None, **extra):
    with guard():
        if job_id and read('update-request.json').get('id') != job_id:
            return False
        saved = read('update.json')
        saved.update(phase=phase, message=message, **extra)
        write('update.json', saved)
    return True


def request(tag=None, app_path=None, rollback=False):
    # Reject dirty checkouts before any install/download or queue mutation.
    ensure_clean(paths.HOME)
    ensure_clean(active_root())
    if not rollback:
        info = check(tag)
        if not info['available']:
            raise UpdateError('현재 버전보다 새로운 정식 릴리스가 없습니다.')
    else:
        info = status()
        if not info.get('previous'):
            raise UpdateError('되돌릴 이전 버전이 없습니다.')
        rollback_root(info['previous'])
    if app_path:
        app = Path(app_path).expanduser().absolute()
        if app.name != 'ReviewBot.app' or not app.is_dir() or app.is_symlink():
            raise UpdateError('업데이트할 ReviewBot.app 경로가 올바르지 않습니다.')
        app_path = str(app)
    elif Path('/Applications/ReviewBot.app').is_dir():
        app_path = '/Applications/ReviewBot.app'
    elif (paths.HOME / 'app/ReviewBot.app').is_dir():
        app_path = str(paths.HOME / 'app/ReviewBot.app')
    if app_path:
        app = Path(app_path)
        if _file('releases').resolve() in app.resolve().parents:
            raise UpdateError('릴리스 보관 경로에서 앱을 직접 실행 중입니다. /Applications에 설치한 앱에서 업데이트하세요.')
        with (app / 'Contents/Info.plist').open('rb') as source:
            if plistlib.load(source).get('CFBundleIdentifier') != 'com.review-bot.app':
                raise UpdateError('ReviewBot 앱 식별자가 일치하지 않습니다.')
    with guard():
        if pending():
            raise UpdateError('이미 예약한 업데이트가 있습니다.')
        # A cancelled downloader may still be unwinding. Do not orphan a new job
        # behind its worker lock when there is no launchd timer to wake it later.
        with guard('update-worker.guard', blocking=False):
            pass
        job = {'id': uuid.uuid4().hex, 'release': info.get('latest'),
               'appPath': app_path, 'rollback': rollback,
               'rollbackTarget': info.get('previous') if rollback else None, 'at': time.time()}
        write('update-request.json', job)
        saved = read('update.json')
        saved.update(phase='queued', message='현재 PR 완료 후 적용할 업데이트를 준비합니다.')
        write('update.json', saved)
        try:
            wake()
        except OSError:
            _file('update-request.json').unlink(missing_ok=True)
            raise UpdateError('업데이트 대기 프로세스를 시작하지 못했습니다.') from None
    return status()


def wake():
    """The worker's advisory lock makes recovery after app/OS restart idempotent."""
    with _file('update.log').open('a') as log:
        subprocess.Popen([sys.executable, str(paths.CODE / 'review-bot'), 'update', '--apply-pending'],
                         env=dict(os.environ, REVIEW_BOT_HOME=str(paths.HOME), REVIEW_BOT_NO_REDIRECT='1'),
                         stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)


def cancel():
    with guard():
        if read('update.json').get('phase') == 'applying':
            raise UpdateError('적용 중에는 취소할 수 없습니다. 완료 후 이전 버전으로 되돌릴 수 있습니다.')
        _file('update-request.json').unlink(missing_ok=True)
        saved = read('update.json')
        saved.update(phase='cancelled', message='업데이트 예약을 취소했습니다.')
        write('update.json', saved)
    return status()


def _point(root):
    pointer = paths.HOME / '.review-bot-current'
    if Path(root) == paths.HOME:
        pointer.unlink(missing_ok=True)
    else:
        temp = paths.HOME / ('.review-bot-current-' + uuid.uuid4().hex)
        temp.symlink_to(root, target_is_directory=True)
        os.replace(temp, pointer)


def recover_activation():
    """Undo an interrupted two-part switch before accepting another activation."""
    journal = read('update-transaction.json')
    if not journal:
        return
    _point(Path(journal['root']))
    app = Path(journal['appPath']) if journal.get('appPath') else None
    backup = Path(journal['appBackup']) if journal.get('appBackup') else None
    if backup and backup.exists():
        if app.exists():
            shutil.rmtree(app)
        os.rename(backup, app)
    replacement = Path(journal['replacement']) if journal.get('replacement') else None
    if replacement and replacement.exists():
        shutil.rmtree(replacement)
    _file('update-transaction.json').unlink(missing_ok=True)


def activate(root, app_path=None, source_app=None):
    """Called only with the review lock held. Roll back both parts on any failure."""
    root = Path(root)
    previous = active_root()
    previous_identity = identity(previous)
    backup = replacement = None
    if app_path:
        app = Path(app_path)
        replacement = app.with_name('.ReviewBot-new-' + uuid.uuid4().hex + '.app')
        backup = app.with_name('.ReviewBot-previous-' + uuid.uuid4().hex + '.app')
        shutil.copytree(source_app or root / 'app/ReviewBot.app', replacement)
    journal = {'root': str(previous), 'appBackup': str(backup) if backup else None,
               'appPath': app_path, 'replacement': str(replacement) if replacement else None}
    write('update-transaction.json', journal)
    try:
        if app_path:
            os.rename(app, backup)
            os.rename(replacement, app)
        _point(root)
        env = dict(os.environ, REVIEW_BOT_HOME=str(paths.HOME), REVIEW_BOT_NO_REDIRECT='1')
        command([sys.executable, str(root / 'review-bot'), 'update', '--self-check'], env=env)
        if app_path:
            check_app(Path(app_path), paths.version(root))
        # Persist rollback information before deleting the crash-recovery journal.
        with guard():
            saved = read('update.json')
            saved['previous'] = {'root': str(previous), 'appBackup': str(backup) if backup else None,
                                 'appPath': app_path, 'identity': previous_identity}
            write('update.json', saved)
    except BaseException:
        recover_activation()
        raise
    finally:
        if replacement and replacement.exists():
            shutil.rmtree(replacement)
    _file('update-transaction.json').unlink(missing_ok=True)
    return {'root': str(previous), 'appBackup': str(backup) if backup else None,
            'appPath': app_path, 'identity': previous_identity}


def apply_pending():
    with guard('update-worker.guard', blocking=False):
        job = read('update-request.json')
        if not job:
            return
        owned = False
        try:
            # If an interrupted activation left a journal, restore under the same
            # review lock before performing any further install work.
            if read('update-transaction.json'):
                if not store.acquire_lock():
                    raise UpdateError('중단된 업데이트 복구를 위해 실행 중인 리뷰가 끝난 뒤 다시 시도하세요.')
                try:
                    recover_activation()
                finally:
                    store.release_lock()
            ensure_clean(paths.HOME)
            ensure_clean(active_root())
            if job.get('rollback'):
                root = rollback_root(job['rollbackTarget'])
            else:
                _progress('downloading', '업데이트 파일을 내려받고 무결성을 확인합니다.', job_id=job['id'])
                root = prepare(job['release'])
            if not _progress('waiting', '현재 PR이 끝나면 업데이트를 적용합니다.', job_id=job['id']):
                return
            deadline = time.monotonic() + 4 * 3600
            while read('update-request.json').get('id') == job['id']:
                if store.acquire_lock():
                    owned = True
                    break
                if time.monotonic() >= deadline:
                    raise UpdateError('현재 리뷰가 4시간 안에 끝나지 않아 예약을 취소했습니다. 다시 시도하세요.')
                time.sleep(5)
            if not owned:
                return
            with guard():
                if read('update-request.json').get('id') != job['id']:
                    return
                saved = read('update.json')
                saved.update(phase='applying', message='앱과 봇을 새 버전으로 전환합니다.')
                write('update.json', saved)
            ensure_clean(paths.HOME)
            ensure_clean(active_root())
            ensure_clean(root)
            if job.get('rollback'):
                rollback_root(job['rollbackTarget'])
            source_app = job['rollbackTarget'].get('appBackup') if job.get('rollback') else None
            previous = activate(root, job.get('appPath'), source_app=source_app)
            _progress('complete', '업데이트를 적용했습니다. 실행 중인 앱은 다시 시작해 주세요.',
                      job_id=job['id'], previous=previous, available=bool(job.get('rollback')), installedAt=time.time())
        except (UpdateError, OSError, ValueError, KeyError) as exc:
            _progress('failed', str(exc), job_id=job['id'])
        finally:
            if owned:
                store.release_lock()
            with guard():
                if read('update-request.json').get('id') == job['id']:
                    _file('update-request.json').unlink(missing_ok=True)

"""PR head 를 격리된 git worktree 로 펼친다.

사용자의 작업 클론을 base 로 쓰되 checkout 은 절대 건드리지 않는다. object 를
공유하므로 큰 저장소도 빠르고, PR ref 는 refs/review-bot/ 네임스페이스에만 써서
사용자의 브랜치·FETCH_HEAD 와 부딪히지 않는다.
"""
import os
import json
import re
from pathlib import Path
import shutil
import subprocess

from lib import resources

from lib.paths import HOME

ROOT = str(HOME)
WT_DIR = os.path.join(ROOT, "state", "wt")
CACHE_DIR = os.path.join(ROOT, "state", "repos")


class WorktreeError(Exception):
    pass


def _git(repo_dir, args, timeout=600, check=True, env=None):
    p = subprocess.run(["git", "-C", repo_dir] + args, capture_output=True,
                       text=True, timeout=timeout, env=env)
    if check and p.returncode != 0:
        raise WorktreeError("git %s: %s" % (
            " ".join(args[:2]), (p.stderr or p.stdout or "").strip()[:300]))
    return p.stdout.strip()


def _cred(env):
    """계정이 고정됐으면 git 도 그 토큰으로 인증하게 한다.

    env 의 GH_TOKEN 을 gh 의 credential helper 가 읽는다. 사용자가 설정해 둔
    다른 helper 를 먼저 비워야 그쪽 캐시가 끼어들지 않는다.
    """
    if not env:
        return []
    return ["-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential"]


def _slug(repo):
    return repo.replace("/", "__")


def _base_repo(repo, local_path, env=None):
    """리뷰의 base 가 될 로컬 저장소. 없으면 봇 캐시에 blobless clone 을 만든다."""
    if local_path and os.path.isdir(os.path.join(local_path, ".git")):
        return local_path
    os.makedirs(CACHE_DIR, exist_ok=True)
    dest = os.path.join(CACHE_DIR, _slug(repo))
    if not os.path.isdir(os.path.join(dest, ".git")):
        p = subprocess.run(
            ["git"] + _cred(env) + ["clone", "--filter=blob:none", "--no-checkout",
                                    "https://github.com/%s.git" % repo, dest],
            capture_output=True, text=True, timeout=1800, env=env)
        if p.returncode != 0:
            raise WorktreeError("clone 실패 %s: %s" % (
                repo, (p.stderr or "").strip()[:300]))
    return dest


def _owner_path(path):
    return Path(WT_DIR).parent / 'wt-owners' / (Path(path).name + '.json')


def _ref_prefix():
    return 'refs/review-bot/' + resources.instance_id() + '/'


def _safe_path(path):
    root = Path(WT_DIR).resolve()
    candidate = Path(path)
    return not candidate.is_symlink() and candidate.resolve().parent == root


def prepare(repo, number, local_path=None, env=None):
    """Persist ownership before acquisition so failures and process death can be recovered."""
    base = _base_repo(repo, local_path, env)
    ref = _ref_prefix() + 'pr-%s' % number
    os.makedirs(WT_DIR, exist_ok=True)
    path = os.path.join(WT_DIR, '%s-%s' % (_slug(repo), number))
    owner_path = _owner_path(path)
    old = resources.read_owner(owner_path)
    if old and (old.get('instance') != resources.instance_id() or resources.alive(old)):
        raise WorktreeError('아직 실행 중인 워크트리 소유자가 있습니다: ' + path)
    owner_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = owner_path.with_suffix('.tmp')
    try:
        temporary.write_text(json.dumps(dict(resources.owner(), repo=repo, number=number,
                                              base=os.path.abspath(base), path=os.path.abspath(path), ref=ref)))
        temporary.replace(owner_path)
    finally:
        temporary.unlink(missing_ok=True)
    try:
        _git(base, _cred(env) + ['fetch', '--quiet', '--no-write-fetch-head', 'origin',
                                 '+refs/pull/%s/head:%s' % (number, ref)], env=env)
        sha = _git(base, ['rev-parse', ref])
        if not sha:
            raise WorktreeError('PR #%s 의 head 를 찾지 못했습니다' % number)
        _remove(base, path)
        _git(base, ['worktree', 'add', '--detach', '--force', path, sha])
        return path, sha, base, ref
    except BaseException as exc:
        errors = cleanup(base, path, ref)
        if errors and isinstance(exc, Exception):
            raise WorktreeError(str(exc) + ' / 정리 미완료: ' + '; '.join(errors)) from exc
        raise


def _remove(base, path):
    if not _safe_path(path):
        raise WorktreeError('봇 전용 경로 밖의 워크트리는 정리하지 않습니다: ' + str(path))
    entries = _git(base, ['worktree', 'list', '--porcelain', '-z'])
    registered = [part[len('worktree '):] for part in entries.split('\0') if part.startswith('worktree ')]
    if any(Path(p).resolve() == Path(path).resolve() for p in registered):
        _git(base, ['worktree', 'remove', '--force', path])
    elif os.path.exists(path):
        shutil.rmtree(path)


def cleanup(base, path, ref=None):
    """Return failures explicitly and keep the ownership journal until cleanup succeeds."""
    try:
        if ref:
            if not ref.startswith(_ref_prefix()) and not re.fullmatch(r'refs/review-bot/pr-\d+', ref):
                raise WorktreeError('다른 소유자의 ref는 정리하지 않습니다: ' + ref)
        record = resources.read_owner(_owner_path(path))
        if record and (record.get('instance') != resources.instance_id() or
                       (resources.alive(record) and record.get('pid') != os.getpid())):
            raise WorktreeError('실행 중이거나 다른 소유자의 워크트리는 정리하지 않습니다: ' + path)
        _remove(base, path)
        if ref:
            _git(base, ['update-ref', '-d', ref])
        _owner_path(path).unlink(missing_ok=True)
        return []
    except (WorktreeError, OSError, subprocess.SubprocessError) as exc:
        return [str(exc)[:400]]


def sweep(repos_cfg, blocked=()):
    """Caller holds the bot lock. Recover dead owners, including ref-only acquisitions."""
    report = {'removed': 0, 'errors': [], 'kept': []}
    if '*' in blocked:
        return report
    jobs, bases, protected_refs = {}, set(), set()
    owner_dir = Path(WT_DIR).parent / 'wt-owners'
    for owner_file in owner_dir.glob('*.json'):
        record = resources.read_owner(owner_file)
        if record.get('instance') != resources.instance_id():
            continue
        if not all(isinstance(record.get(k), str) and record[k] for k in ('path', 'base', 'ref')) or not _safe_path(record['path']):
            report['errors'].append('워크트리 소유 기록을 확인할 수 없습니다: ' + str(owner_file))
            continue
        if resources.alive(record) or resources.owner_key(record) in blocked:
            protected_refs.add(record.get('ref'))
            report['kept'].append(record['path'])
            continue
        jobs[record['path']] = (record['base'], record.get('ref'))
        bases.add(record['base'])
    for repo, entry in (repos_cfg or {}).items():
        local = os.path.expanduser((entry or {}).get('localPath') or '')
        base = local if local and os.path.isdir(os.path.join(local, '.git')) else os.path.join(CACHE_DIR, _slug(repo))
        if not os.path.isdir(os.path.join(base, '.git')):
            continue
        bases.add(base)
        for path in Path(WT_DIR).glob(_slug(repo) + '-*'):
            number = path.name.rpartition('-')[2]
            if not number.isdigit() or not _safe_path(path): continue
            owner_file = _owner_path(path)
            if owner_file.exists(): continue  # Journal above determines liveness/ownership.
            jobs[str(path)] = (base, 'refs/review-bot/pr-' + number)
    for path, (base, ref) in jobs.items():
        errors = cleanup(base, path, ref)
        report['errors'].extend(errors)
        if not errors:
            report['removed'] += 1
    # A crash may occur after ref acquisition but before worktree creation.
    for base in bases:
        try:
            refs = _git(base, ['for-each-ref', '--format=%(refname)', _ref_prefix()]).splitlines()
            for ref in refs:
                if ref in protected_refs: continue
                if any(ref == r for _, r in jobs.values()): continue  # Failed cleanup retains its journal.
                _git(base, ['update-ref', '-d', ref])
                report['removed'] += 1
        except (WorktreeError, OSError, subprocess.SubprocessError) as exc:
            report['errors'].append(str(exc)[:400])
    return report


def file_history(base, sha, paths, per_file=5, max_files=10):
    """변경된 파일들이 그동안 어떻게 바뀌어 왔는지 — {경로: [커밋 줄]}.

    리뷰어에게 Bash 를 주는 대신 봇이 여기서 읽어 프롬프트에 실어 준다. 워크트리에는
    남의 PR 코드가 펼쳐져 있어서, 거기서 명령이 돌 수 있게 하는 것과 신뢰되는 쪽이
    읽어서 건네주는 것은 위험이 전혀 다르다.

    blobless clone 에서도 싸다 — 커밋과 트리만 있으면 되고 blob 은 건드리지 않는다.
    """
    out = {}
    for p in list(paths)[:max_files]:
        try:
            log = _git(base, ["log", "-n", str(per_file), "--no-merges",
                              "--format=%h %ad %an — %s", "--date=short",
                              sha, "--", p], timeout=60, check=False)
        except (WorktreeError, subprocess.TimeoutExpired, OSError):
            continue          # 맥락은 있으면 좋은 것이지 리뷰를 막을 이유가 아니다
        if log:
            # 커밋 제목은 길이 제한이 없다. 한 줄이 프롬프트를 잡아먹지 않게 자른다.
            out[p] = [l[:200] for l in log.splitlines()[:per_file]]
    return out


def complete_patches(base, head_sha, base_sha, files, env=None):
    """Recover missing/truncated API patches from the pinned PR merge base.

    git's textconv/external diff drivers are disabled: PR configuration must not
    execute programs while the bot prepares review data.
    """
    from lib.batches import HEADER

    def complete(item):
        patch = item.get('patch') or ''
        added = deleted = 0
        in_hunk = False
        for line in patch.splitlines():
            if HEADER.match(line):
                in_hunk = True
            elif in_hunk:
                added += line.startswith('+')
                deleted += line.startswith('-')
        return bool(patch) and added == (item.get('additions') or 0) and deleted == (item.get('deletions') or 0)

    missing = [item for item in files if not complete(item)]
    if not missing:
        return files
    if not base_sha:
        raise WorktreeError('완전한 diff 복원에 필요한 base SHA가 없습니다')
    known = _git(base, ['rev-parse', '--verify', base_sha + '^{commit}'], check=False)
    if known != base_sha:
        _git(base, _cred(env) + ['fetch', '--quiet', '--no-write-fetch-head', 'origin', base_sha], env=env)
    merge_base = _git(base, ['merge-base', base_sha, head_sha])
    result = []
    for item in files:
        if complete(item):
            result.append(item)
            continue
        paths = [item['filename']]
        if item.get('previous_filename'):
            paths.append(item['previous_filename'])
        raw = _git(base, ['--literal-pathspecs', 'diff', '--no-ext-diff', '--no-textconv',
                          '--find-renames', '--unified=3', merge_base, head_sha, '--'] + paths)
        lines = raw.splitlines(keepends=True)
        start = next((i for i, line in enumerate(lines) if HEADER.match(line)), None)
        recovered = dict(item, patch=''.join(lines[start:]) if start is not None else '')
        if start is None:
            # Empty package markers have a real diff, but no text hunk. Verify
            # the pinned blob before treating the metadata as complete evidence.
            empty_added = (item.get('status') == 'added'
                           and item.get('additions') == 0 and item.get('deletions') == 0
                           and any(line.startswith('new file mode ') for line in lines)
                           and _git(base, ['cat-file', '-s', head_sha + ':' + item['filename']]) == '0')
            if empty_added:
                recovered['patch'] = raw
                recovered.pop('_patch_error', None)
            else:
                recovered['_patch_error'] = ('바이너리 diff' if 'Binary files ' in raw else '텍스트 hunk 없음 (이름·모드 변경 등)')
        elif not complete(recovered):
            raise WorktreeError('API 파일 통계와 고정 커밋 diff가 다릅니다: ' + item['filename'])
        result.append(recovered)
    return result

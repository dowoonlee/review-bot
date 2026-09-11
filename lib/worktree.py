"""PR head 를 격리된 git worktree 로 펼친다.

사용자의 작업 클론을 base 로 쓰되 checkout 은 절대 건드리지 않는다. object 를
공유하므로 큰 저장소도 빠르고, PR ref 는 refs/review-bot/ 네임스페이스에만 써서
사용자의 브랜치·FETCH_HEAD 와 부딪히지 않는다.
"""
import os
import shutil
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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


def prepare(repo, number, local_path=None, env=None):
    """(worktree 경로, head sha, base, ref) 를 준다. 끝나면 cleanup 해야 한다.

    env 는 gh.env_for(account) — 계정이 고정됐을 때 fetch 인증에 쓴다.
    """
    base = _base_repo(repo, local_path, env)
    ref = "refs/review-bot/pr-%s" % number
    _git(base, _cred(env) + ["fetch", "--quiet", "origin",
                             "+refs/pull/%s/head:%s" % (number, ref)], env=env)
    sha = _git(base, ["rev-parse", ref])
    if not sha:
        raise WorktreeError("PR #%s 의 head 를 찾지 못했습니다" % number)

    os.makedirs(WT_DIR, exist_ok=True)
    path = os.path.join(WT_DIR, "%s-%s" % (_slug(repo), number))
    _remove(base, path)
    _git(base, ["worktree", "add", "--detach", "--force", path, sha])
    return path, sha, base, ref


def _remove(base, path):
    if os.path.exists(path):
        subprocess.run(["git", "-C", base, "worktree", "remove", "--force", path],
                       capture_output=True, text=True, timeout=120)
    if os.path.exists(path):
        shutil.rmtree(path, ignore_errors=True)


def cleanup(base, path, ref=None):
    """워크트리와 봇 전용 ref 를 지운다. 실패해도 예외를 올리지 않는다."""
    try:
        _remove(base, path)
        _git(base, ["worktree", "prune"], check=False)
        if ref:
            _git(base, ["update-ref", "-d", ref], check=False)
    except (WorktreeError, OSError, subprocess.SubprocessError):
        pass


def sweep(repos_cfg):
    """이전 실행이 비정상 종료되며 남긴 워크트리와 ref 를 치운다.

    OOM·강제 종료·재부팅으로 프로세스가 SIGKILL 되면 cleanup 이 돌지 못해
    워크트리가 남는다. 남은 워크트리는 디스크만 먹는 게 아니라 다음 실행의
    `worktree add` 를 막으므로, 틱을 시작할 때 먼저 쓸어낸다.
    """
    if not os.path.isdir(WT_DIR):
        return 0
    swept = 0
    for name in sorted(os.listdir(WT_DIR)):
        path = os.path.join(WT_DIR, name)
        if not os.path.isdir(path):
            continue
        slug, _, num = name.rpartition("-")
        repo = slug.replace("__", "/")
        entry = (repos_cfg or {}).get(repo) or {}
        lp = os.path.expanduser(entry.get("localPath") or "")
        base = (lp if lp and os.path.isdir(os.path.join(lp, ".git"))
                else os.path.join(CACHE_DIR, slug))
        if not os.path.isdir(os.path.join(base, ".git")):
            shutil.rmtree(path, ignore_errors=True)
            swept += 1
            continue
        cleanup(base, path, "refs/review-bot/pr-%s" % num if num.isdigit() else None)
        swept += 1
    return swept


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

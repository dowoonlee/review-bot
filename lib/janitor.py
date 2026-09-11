"""리뷰가 남기는 부산물을 치운다.

워크트리 말고도 남는 것이 있다. 가장 큰 것은 claude 세션 transcript 다 —
헤드리스 실행도 보통 세션과 똑같이 `~/.claude/projects/<인코딩된 cwd>/` 에
기록을 남기는데, 봇의 워크트리 경로에는 PR 번호가 들어 있어 PR 마다 새 디렉터리가
생긴다. PR 한 건이 수 MB 이므로 그냥 두면 조용히 쌓인다.
"""
import os
import re
import shutil
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.join(ROOT, "state")
WT_DIR = os.path.join(STATE_DIR, "wt")
DRAFTS = os.path.join(STATE_DIR, "drafts")
CACHE_DIR = os.path.join(STATE_DIR, "repos")
PROJECTS = os.path.expanduser("~/.claude/projects")


def _encode_path(path):
    """claude 가 cwd 를 프로젝트 디렉터리 이름으로 바꾸는 규칙."""
    return re.sub(r"[/._]", "-", path)


def session_dir(wt_path):
    """그 워크트리에서 돌린 claude 세션들이 쌓이는 디렉터리."""
    return os.path.join(PROJECTS, _encode_path(wt_path))


def drop_session(wt_path):
    """리뷰가 성공했으면 그 세션 기록은 남길 이유가 없다."""
    d = session_dir(wt_path)
    if os.path.isdir(d):
        size = _du(d)
        shutil.rmtree(d, ignore_errors=True)
        return size
    return 0


def sweep_sessions(keep_days=3):
    """실패한 리뷰가 남긴 세션을 보존 기간이 지나면 치운다.

    실패한 리뷰의 transcript 는 프롬프트를 고칠 단서라 바로 지우지 않는다.
    """
    if not os.path.isdir(PROJECTS):
        return 0, 0
    prefix = _encode_path(WT_DIR)
    cutoff = time.time() - keep_days * 86400
    n, freed = 0, 0
    for name in os.listdir(PROJECTS):
        if not name.startswith(prefix):
            continue                      # 봇이 만든 것만 건드린다
        d = os.path.join(PROJECTS, name)
        if not os.path.isdir(d) or os.path.getmtime(d) > cutoff:
            continue
        freed += _du(d)
        shutil.rmtree(d, ignore_errors=True)
        n += 1
    return n, freed


def prune_drafts(keep_days=14):
    if not os.path.isdir(DRAFTS):
        return 0
    cutoff = time.time() - keep_days * 86400
    n = 0
    for name in os.listdir(DRAFTS):
        p = os.path.join(DRAFTS, name)
        if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
            os.remove(p)
            n += 1
    return n


def trim_runs(path, max_lines=2000):
    """이력은 최근 것만 남긴다 — 무한히 자라면 runs 조회가 느려진다."""
    if not os.path.exists(path):
        return 0
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    if len(lines) <= max_lines:
        return 0
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines[-max_lines:])
    return len(lines) - max_lines


def rotate_logs(max_bytes=5_000_000):
    """launchd 가 붙여 쓰는 stdout/stderr 는 스스로 줄지 않는다."""
    n = 0
    for name in ("bot.log", "bot.err"):
        p = os.path.join(STATE_DIR, name)
        if os.path.exists(p) and os.path.getsize(p) > max_bytes:
            shutil.move(p, p + ".1")      # 한 세대만 남긴다
            n += 1
    return n


def prune_reviewed(state, keep_days=90):
    """닫힌 지 오래된 PR 기록은 중복 방지에 더 쓸모가 없다."""
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%S",
                           time.localtime(time.time() - keep_days * 86400))
    stale = [k for k, v in state.items()
             if isinstance(v, dict) and (v.get("at") or "") < cutoff]
    for k in stale:
        del state[k]
    return len(stale)


def sweep_cache_repos(known_repos):
    """화이트리스트에서 빠진 저장소의 캐시 클론을 치운다."""
    if not os.path.isdir(CACHE_DIR):
        return 0, 0
    keep = set((r or "").replace("/", "__") for r in (known_repos or {}))
    n, freed = 0, 0
    for name in os.listdir(CACHE_DIR):
        if name in keep:
            continue
        d = os.path.join(CACHE_DIR, name)
        if os.path.isdir(d):
            freed += _du(d)
            shutil.rmtree(d, ignore_errors=True)
            n += 1
    return n, freed


def _du(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return "%.0f%s" % (n, unit)
        n /= 1024.0
    return "%.1fTB" % n

"""리뷰가 남기는 부산물을 치운다.

워크트리 말고도 남는 것이 있다. 가장 큰 것은 claude 세션 transcript 다 —
헤드리스 실행도 보통 세션과 똑같이 `~/.claude/projects/<인코딩된 cwd>/` 에
기록을 남기는데, 봇의 워크트리 경로에는 PR 번호가 들어 있어 PR 마다 새 디렉터리가
생긴다. PR 한 건이 수 MB 이므로 그냥 두면 조용히 쌓인다.
"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import signal
import time

from lib import resources

from lib.paths import HOME

ROOT = str(HOME)
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


def reap_handoff_shells(min_age=600, listing=None, kill=os.kill):
    """Kill agent handoff shells the app left behind after their iTerm tab closed.

    The app starts each agent as `zsh -lic '... _review_bot_prompt ...'` in an iTerm
    tab. Such a shell that has lost its terminal and parent and runs nothing is dead
    weight; one that still has a terminal or a running agent is somebody's session.
    """
    if listing is None:
        proc = subprocess.run(['ps', '-axo', 'pid=,ppid=,tty=,etime=,command='],
                              capture_output=True, text=True, timeout=10)
        listing = proc.stdout if proc.returncode == 0 else ''
    rows = []
    parents = set()
    for line in listing.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        pid, ppid, tty, etime, command = parts
        parents.add(ppid)
        rows.append((pid, ppid, tty, etime, command))
    reaped = 0
    for pid, ppid, tty, etime, command in rows:
        if (ppid != '1' or tty not in ('??', '?') or '_review_bot_prompt' not in command or
                not command.startswith('/bin/zsh -lic') or pid in parents or _seconds(etime) < min_age):
            continue
        try:
            kill(int(pid), signal.SIGTERM)
            reaped += 1
        except (ProcessLookupError, PermissionError, ValueError):
            pass
    return reaped


def _seconds(etime):
    """ps etime `[[dd-]hh:]mm:ss` in seconds."""
    days, _, clock = etime.rpartition('-')
    total = 0
    for part in clock.split(':'):
        total = total * 60 + int(part)
    return total + int(days or 0) * 86400


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


def prune_day_logs(keep_days=30):
    """날짜별 틱 로그를 보존 기간만큼만 남긴다 — (지운 개수, 바이트)."""
    logs = os.path.join(STATE_DIR, "logs")
    if not os.path.isdir(logs):
        return 0, 0
    cutoff = time.strftime("%Y-%m-%d",
                           time.localtime(time.time() - keep_days * 86400))
    n, freed = 0, 0
    for name in sorted(os.listdir(logs)):
        if not name.endswith(".log") or len(name) != 14:
            continue
        if name[:-4] >= cutoff:
            continue
        path = os.path.join(logs, name)
        try:
            freed += os.path.getsize(path)
            os.remove(path)
            n += 1
        except OSError:
            pass
    return n, freed


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


def _evidence_run(path, root):
    """The run directory (repo/number/sha/run) that holds an evidence path, if any."""
    try:
        parts = Path(path).relative_to(root).parts
    except ValueError:
        return None
    return root.joinpath(*parts[:4]) if len(parts) >= 4 else None


def prune_superseded(checkpoint, blocked=()):
    """Delete this PR's earlier runs as soon as a newer review has replaced them.

    A review leaves one evidence run per attempt and per head; only the runs the
    saved checkpoint still points at (its directory, recovery attempts, reused test
    reports and screenshots) can be read again. The rest would sit for evidenceDays.
    """
    if '*' in blocked:
        return 0
    root = Path(STATE_DIR, "evidence")
    runtime = ((checkpoint or {}).get('payload') or {}).get('runtime') or {}
    referenced = {_evidence_run(m, root) for m in re.findall(r'"(/[^"]+)"', json.dumps(runtime))}
    referenced.discard(None)
    scope = referenced and next(iter(referenced)).parent.parent
    if not scope or not scope.is_dir() or scope.is_symlink():
        return 0
    # Runs after the checkpoint was saved belong to a review that has not replaced
    # it (still running, or failed): they are the evidence of what went wrong.
    saved = checkpoint.get('updated') or 0
    removed = 0
    for run in scope.glob('*/*'):
        if run in referenced or not run.is_dir() or run.is_symlink() or run.parent.is_symlink():
            continue
        if max([run.stat().st_mtime] + [p.stat().st_mtime for p in run.iterdir() if not p.is_symlink()]) >= saved:
            continue
        owner = resources.read_owner(run / 'owner.json')
        if owner.get('instance') and (owner['instance'] != resources.instance_id() or
                                     resources.alive(owner) or resources.owner_key(owner) in blocked):
            continue  # Another bot instance, or a run that is still executing.
        shutil.rmtree(run)
        removed += 1
        try:
            run.parent.rmdir()
        except OSError:
            pass
    return removed


def prune_evidence(keep_days=14, blocked=()):
    """Reap old interrupted runs too; preserve live/unknown Docker owners."""
    if '*' in blocked:
        return 0
    cutoff = time.time() - keep_days * 86400
    removed = 0
    root = Path(STATE_DIR, "evidence")
    for run in root.glob('*/*/*/*'):
        if not run.is_dir() or any(p.is_symlink() for p in (run, *run.parents) if p.is_relative_to(root)):
            continue
        owner = resources.read_owner(run / 'owner.json')
        if owner.get('instance') and (owner['instance'] != resources.instance_id() or
                                     resources.alive(owner) or resources.owner_key(owner) in blocked):
            continue
        result = run / 'result.json'
        if result.is_symlink():
            continue
        latest = result.stat().st_mtime if result.exists() else max(
            [run.stat().st_mtime] + [p.stat().st_mtime for p in run.rglob('*') if not p.is_symlink()])
        # Archives are transport temporaries, never review evidence.
        if owner.get('instance') == resources.instance_id():
            for name in ('source.tar', 'screenshots.tar', 'baseline/source.tar'):
                temporary = run / name
                if not temporary.parent.is_symlink():
                    temporary.unlink(missing_ok=True)
        if latest >= cutoff:
            continue
        shutil.rmtree(run)
        removed += 1
        parent = run.parent
        while parent != root and parent.is_relative_to(root):
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
    return removed

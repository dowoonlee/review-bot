"""처리 이력 — 같은 PR 을 같은 커밋으로 두 번 리뷰하지 않기 위한 지문."""
import json
import fcntl
import os
import time
from contextlib import contextmanager

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.join(ROOT, "state")
REVIEWED = os.path.join(STATE_DIR, "reviewed.json")
LOG = os.path.join(STATE_DIR, "runs.jsonl")
# 반증 단계가 매긴 confidence 를 지적 단위로 쌓아 둔다. 필터에는 쓰지 않는다 —
# 이 점수가 실제로 무엇을 가르는지 데이터가 쌓인 뒤에 판단하려고 남기는 것이다.
VERIFY_LOG = os.path.join(STATE_DIR, "verify.jsonl")
LOG_TEXT = os.path.join(STATE_DIR, "bot.log")   # launchd 가 stdout 을 붙이는 그 파일
# 날짜별 틱 로그. launchd 는 bot.log 하나에 파일 핸들을 잡고 있어 그 파일 자체를
# 쪼갤 수 없으므로, 봇이 여기에 따로 적는다. bot.log 는 launchd 의 raw 출력으로
# 남겨 두고(예기치 못한 stderr 도 거기 떨어진다), 사람이 읽는 것은 이쪽이다.
LOGS_DIR = os.path.join(STATE_DIR, "logs")
PAUSED = os.path.join(STATE_DIR, "PAUSED")
DRAFTS = os.path.join(STATE_DIR, "drafts")


def _ensure():
    os.makedirs(STATE_DIR, exist_ok=True)
    os.makedirs(DRAFTS, exist_ok=True)


def is_paused():
    return os.path.exists(PAUSED)


def pause(reason=""):
    _ensure()
    with open(PAUSED, "w", encoding="utf-8") as f:
        f.write("%s\n%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), reason))


def resume():
    if os.path.exists(PAUSED):
        os.remove(PAUSED)


def load():
    if not os.path.exists(REVIEWED):
        return {}
    try:
        with open(REVIEWED, encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError):
        return {}


def save(data):
    _ensure()
    tmp = REVIEWED + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, REVIEWED)


def commit(state, mutate):
    """reviewed.json 을 잠근 채 다시 읽어 `mutate` 를 적용하고 저장한다.

    틱은 처리 이력을 시작할 때 한 번 읽어 들고 다니면서 PR 하나를 끝낼 때마다 그
    사본을 통째로 저장한다. 그 사이 `publish` 가 다른 PR 의 기록을 "게시함" 으로
    고쳤다면, 틱의 다음 저장이 낡은 사본으로 그 기록을 덮어 게시가 없던 일이 된다.
    그래서 쓰기는 들고 있던 사본이 아니라 지금 파일에 있는 내용 위에 하고, 들고
    있던 `state` 는 저장된 내용으로 바꿔 준다. `mutate` 의 반환값을 그대로 돌려준다.
    """
    _ensure()
    with open(REVIEWED + ".lock", "a+") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        fresh = load()
        result = mutate(fresh)
        save(fresh)
    if state is not None and state is not fresh:
        state.clear()
        state.update(fresh)
    return result


def key(repo, number):
    return "%s#%s" % (repo, number)


def already_done(data, repo, number, sha, rereview_on_new, dry_run=False,
                 requested_at=None):
    """이 커밋을 이미 처리했는가.

    dry-run 으로 남긴 초안은 아직 아무에게도 닿지 않았다. 게시 모드로 바꾼 뒤에도
    "이미 리뷰함" 으로 건너뛰면 그 PR 은 새 커밋이 올라오기 전까지 영원히 게시되지
    않으므로, 초안뿐인 기록은 dry-run 이 켜져 있는 동안만 처리 완료로 친다.

    `requested_at` 은 나에게 온 마지막 리뷰 요청의 시각이다. 커밋 지문만으로
    가르면 **같은 커밋 그대로 온 재요청**이 보이지 않는다 — 리뷰를 게시하면
    GitHub 이 요청을 걷어 가므로, 그 뒤 다시 요청이 들어왔다는 것은 사람이 한 번
    더 봐 달라고 말한 것이다. 그런데도 봇은 새 커밋이 올라올 때까지 매 틱
    "이미 리뷰함" 으로 건너뛰었다. 그래서 답한 요청의 시각을 기록에 남기고,
    그것과 다른 요청이 와 있으면 다시 본다.

    시각을 견주지 않고 "요청이 열려 있으면 재요청" 으로 가르지 않는 이유는, 그
    판정이 답한 뒤에도 계속 참이면 매 틱 돈을 쓰는 되돌이가 되기 때문이다.
    `requested_at` 이 `None` 이면(조회 실패·요청 이력 없음) 예전처럼 커밋만 본다 —
    돈이 드는 쪽으로 기울지 않는 것이 안전하다.
    """
    rec = data.get(key(repo, number))
    if not rec:
        return False
    if rec.get('pending'):
        return False  # Environment/CI waiting is not a completed review.
    if rereview_on_new and rec.get("sha") != sha:
        return False
    if requested_at and requested_at != rec.get('requestedAt'):
        return False
    if rec.get('discarded'):
        return True
    if rec.get("posted") is False:
        # 그 커밋 그대로 게시를 기다리는 초안이 있으면 다시 리뷰하지 않는다 — 사람이
        # 읽고 게시하면 될 일이라 돈을 두 번 쓸 이유가 없다. 초안이 사라졌거나(게시함)
        # 좌표가 낡았을 때만, 그리고 게시 모드일 때만 다시 본다.
        if has_pending_draft(repo, number, sha):
            return True
        if not dry_run:
            return False
    return True


def skip_key(repo, number):
    # `key()` 와 겹치지 않는 이름이어야 한다 — 겹치면 `already_done` 이 건너뛴
    # PR 을 "리뷰 완료" 로 읽는다.
    return "skip:%s#%s" % (repo, number)


def skip_announced(data, repo, number, sha, reason):
    """이 커밋의 이 사유를 이미 알렸는가.

    크기 상한에 걸린 PR 은 새 커밋이 올라오기 전까지 매 틱 같은 자리에서 걸린다.
    그때마다 알림을 울리면 사람은 알림을 끄고, 그러면 처음부터 안 알린 것과
    같아진다. 그래서 커밋 하나당 한 번만 말한다.
    """
    rec = data.get(skip_key(repo, number))
    return bool(rec) and rec.get("sha") == sha and rec.get("reason") == reason


def mark_skip_announced(data, repo, number, sha, reason):
    data[skip_key(repo, number)] = {
        "sha": sha, "reason": reason,
        "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    return data


def has_pending_draft(repo, number, sha=None):
    """게시를 기다리는 초안이 있는가. sha 를 주면 그 커밋의 것인지까지 본다."""
    path = draft_payload_path(repo, number)
    if not os.path.exists(path):
        return False
    if sha is None:
        return True
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("sha") == sha
    except (ValueError, OSError):
        return False


def mark(data, repo, number, sha, **fields):
    prev = data.get(key(repo, number)) or {}
    rec = {"sha": sha, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    rec.update(fields)
    # 관측하지 못한 요청 시각(`None`)으로 기록을 덮으면, 다음 틱이 열려 있는 요청을
    # 재요청으로 읽어 같은 커밋을 또 리뷰한다 — 리뷰 한 번이 몇 달러다. 모를 때는
    # 지운 것이 아니라 모르는 것이므로 이전에 답한 요청을 그대로 지킨다.
    if rec.get("requestedAt") is None and prev.get("requestedAt"):
        rec["requestedAt"] = prev["requestedAt"]
    data[key(repo, number)] = rec
    return data


def day_log_path(day=None):
    return os.path.join(LOGS_DIR, "%s.log" % (day or time.strftime("%Y-%m-%d")))


def append_day_log(line):
    """오늘 날짜 파일에 한 줄 붙인다. 실패해도 리뷰를 막지 않는다."""
    try:
        os.makedirs(LOGS_DIR, exist_ok=True)
        with open(day_log_path(), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def log_days():
    """로그가 있는 날짜, 최근 것부터."""
    if not os.path.isdir(LOGS_DIR):
        return []
    days = [f[:-4] for f in os.listdir(LOGS_DIR)
            if f.endswith(".log") and len(f) == 14]
    return sorted(days, reverse=True)


def read_day_log(day=None, max_lines=4000):
    """하루치 로그 줄. 없으면 빈 목록."""
    path = day_log_path(day)
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            lines = [l.rstrip("\n") for l in f]
    except OSError:
        return []
    return lines[-max_lines:]


def log_run(record):
    _ensure()
    record = dict(record)
    record.setdefault("at", time.strftime("%Y-%m-%dT%H:%M:%S"))
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def log_verify(repo, number, sha, rows):
    """반증 결과를 지적 하나당 한 줄로 남긴다.

    `fate` 가 그 지적이 끝내 어떻게 됐는지다 — rejected(반증됨) / inline / body /
    drop / demoted. confidence 와 fate 를 나란히 쌓아야 나중에 "몇 점부터 쓸모가
    있었나" 를 점수 분포로 답할 수 있다.
    """
    if not rows:
        return
    _ensure()
    at = time.strftime("%Y-%m-%dT%H:%M:%S")
    with open(VERIFY_LOG, "a", encoding="utf-8") as f:
        for r in rows:
            v = r.get("_verify") or {}
            f.write(json.dumps({
                "at": at, "repo": repo, "number": number, "sha": sha,
                "agent": r.get("agent"), "path": r.get("path"), "line": r.get("line"),
                "scope": r.get("_scope"),
                "severity": r.get("severity"),
                "severity_was": v.get("severity_was"),
                "verdict": v.get("verdict"), "confidence": v.get("confidence"),
                "error": v.get("error"),
                "title": (r.get("title") or "")[:120],
                "reason": (v.get("reason") or "")[:300],
                "fate": r.get("_fate"),
            }, ensure_ascii=False) + "\n")


def read_verify():
    """반증 로그 전체. 임계값을 정할 때 이 분포를 본다."""
    if not os.path.exists(VERIFY_LOG):
        return []
    out = []
    with open(VERIFY_LOG, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    return out


def read_runs():
    """이력 전체를 읽는다. 깨진 줄은 건너뛴다."""
    if not os.path.exists(LOG):
        return []
    out = []
    with open(LOG, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def write_runs(rows):
    """이력을 통째로 다시 쓴다. 부분 삭제는 이 방법뿐이다 (append-only 파일이라)."""
    _ensure()
    tmp = LOG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, LOG)


def draft_path(repo, number):
    _ensure()
    return os.path.join(DRAFTS, "%s-%s.md" % (repo.replace("/", "__"), number))


def draft_payload_path(repo, number):
    """초안을 나중에 그대로 게시하기 위한 기계용 사본.

    .md 는 사람이 읽는 것이고, 이 .json 이 게시에 쓰인다. 검토 후 게시할 때 리뷰를
    다시 돌리지 않으려면 그때 만들어진 body 와 인라인 좌표가 그대로 남아 있어야 한다.
    """
    _ensure()
    return os.path.join(DRAFTS, "%s-%s.json" % (repo.replace("/", "__"), number))


def pending_drafts():
    """게시를 기다리는 초안들 — (repo, number, payload) 목록."""
    _ensure()
    out = []
    for name in sorted(os.listdir(DRAFTS)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(DRAFTS, name), encoding="utf-8") as f:
                data = json.load(f)
        except (ValueError, OSError):
            continue
        if data.get("repo") and data.get("number"):
            out.append(data)
    return out


SCHEDULE = os.path.join(STATE_DIR, "schedule.json")


def set_schedule_anchor(reason, interval_sec=None):
    """다음 틱을 셀 기준점. launchd 는 다음 발사 시각을 알려주지 않는다.

    타이머가 다시 시작되는 순간은 둘뿐이다 — launchd 가 틱을 띄웠을 때, 그리고
    job 을 다시 올렸을 때(주기 변경). 손으로 부른 틱은 launchd 의 시계를 건드리지
    않으므로 여기 남기지 않는다.
    """
    _ensure()
    data = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "reason": reason}
    if interval_sec:
        data["intervalSec"] = int(interval_sec)
    tmp = SCHEDULE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, SCHEDULE)


def read_schedule():
    try:
        with open(SCHEDULE, encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError):
        return None


CURRENT = os.path.join(STATE_DIR, "current.json")


def set_current(data):
    """지금 리뷰 중인 PR 이 무엇인지 남긴다.

    워크트리 경로에는 저장소와 번호밖에 없어서, 누가 낸 PR 인지·누가 리뷰를
    요청했는지는 여기서만 알 수 있다. 리뷰가 끝나면 지운다.
    """
    _ensure()
    tmp = CURRENT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CURRENT)


def clear_current():
    try:
        os.remove(CURRENT)
    except OSError:
        pass


def read_current():
    try:
        with open(CURRENT, encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError):
        return None


LOCK = os.path.join(STATE_DIR, "tick.lock")
_LOCK_HANDLE = None
_LOCK_OWNER = None
_LOCK_MARKER = None


def acquire_lock():
    """틱이 겹쳐 도는 것을 막는다.

    launchd 는 이전 실행이 끝났는지 보지 않고 StartInterval 마다 새로 띄운다.
    리뷰 한 건이 15분을 넘길 수 있으므로 겹침은 예외가 아니라 일상이다.
    """
    global _LOCK_HANDLE, _LOCK_OWNER, _LOCK_MARKER
    if _LOCK_HANDLE is not None:
        return False
    _ensure()
    # The guard inode is persistent. Unlinking a flock file creates a race where
    # contenders can lock different inodes. tick.lock remains the UI's PID marker.
    guard = open(LOCK + '.guard', 'a+')
    try:
        fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        guard.close()
        return False
    try:
        # Respect an older bot that does not yet hold the advisory lock.
        if os.path.exists(LOCK):
            with open(LOCK, encoding='utf-8') as f:
                parts = f.read().split()
            if len(parts) < 3 or parts[2] != 'v2':
                try:
                    pid = int(parts[0]) if parts else 0
                except ValueError:
                    pid = 0
                if pid > 0:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        pass
                    except PermissionError:
                        guard.close()
                        return False
                    else:
                        guard.close()
                        return False
        with open(LOCK, 'w', encoding='utf-8') as f:
            f.write('%d %s v2\n' % (os.getpid(), time.strftime('%Y-%m-%dT%H:%M:%S')))
    except BaseException:
        guard.close()
        raise
    _LOCK_HANDLE, _LOCK_OWNER, _LOCK_MARKER = guard, os.getpid(), LOCK
    return True


def release_lock():
    global _LOCK_HANDLE, _LOCK_OWNER, _LOCK_MARKER
    if _LOCK_HANDLE is None or _LOCK_OWNER != os.getpid():
        return
    try:
        try:
            os.remove(_LOCK_MARKER)
        except FileNotFoundError:
            pass
    finally:
        _LOCK_HANDLE.close()
        _LOCK_HANDLE = _LOCK_OWNER = _LOCK_MARKER = None


def pr_lock_path(repo, number):
    return os.path.join(STATE_DIR, "locks", "%s-%s.lock" % (repo.replace("/", "__"), number))


@contextmanager
def pr_lock(repo, number):
    """PR 하나를 잡는다. 잡았으면 True, 다른 명령이 잡고 있으면 False 를 낸다.

    틱 전체를 잠그는 `acquire_lock` 과 다르다. 초안 게시와 이벤트 변경은 그 PR 의
    초안 파일과 처리 이력 한 줄만 건드리므로, 틱이 **다른** PR 을 리뷰하는 동안에도
    할 수 있어야 한다. 겹치면 안 되는 것은 같은 PR 을 리뷰하는 틱과 게시가 동시에
    초안을 쓰는 경우뿐이라, 그 범위만 PR 단위로 잠근다. 틱은 PR 을 처리하는 동안,
    게시·이벤트 변경은 파일을 읽고 쓰는 동안 이 lock 을 잡는다.

    lock 파일은 지우지 않는다 — flock 파일을 unlink 하면 서로 다른 inode 를 잠근
    두 쪽이 동시에 들어오는 틈이 생긴다 (`acquire_lock` 의 guard 와 같은 이유).
    """
    path = pr_lock_path(repo, number)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    handle = open(path, "a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        yield False
        return
    try:
        yield True
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            handle.close()

"""What became of the bot's findings: the resolution rate.

A review prompt or a new stage can only be judged by what happens to the findings it
produces. Once a reviewed PR is merged or closed, each inline finding the bot posted
is classified from the record GitHub keeps on its thread: whether the code under it
changed afterwards, whether it was resolved, and what was answered. A finding that
was fixed before merge was worth posting; one that was declined or ignored was not.

Only inline findings can be followed this way. Findings in the review body have no
thread, and nothing mechanical says what happened to them.
"""
import fcntl
import json
import os
import re
import time

from lib import config as cfg_mod
from lib import github as gh
from lib import reviewer, store

MARKER = "<!-- review-bot -->"
OUTCOMES = ("fixed", "declined", "deferred", "unanswered")
LABEL = {"fixed": "고쳐짐", "declined": "거절·의도된 동작", "deferred": "후속으로 미룸",
         "unanswered": "무응답", "unknown": "판정 못 함"}
_HEAD = re.compile(r"\*\*\[(P1|P2|P3|altitude)\]\*\*\s*(.*)")
_ACTIONS = ("머지 전 수정 필요", "선택적 개선", "확인 필요")
PROMPT = os.path.join(reviewer.ROOT, "prompts", "outcome.md")


def path():
    return os.path.join(store.STATE_DIR, "outcomes.json")


def load():
    try:
        with open(path(), encoding="utf-8") as stream:
            data = json.load(stream)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data):
    os.makedirs(store.STATE_DIR, exist_ok=True)
    temporary = path() + ".tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=1)
    os.replace(temporary, path())


def parse_comment(body):
    """(priority, title, action) of a comment the bot posted, or None when it is not one."""
    if MARKER not in (body or ""):
        return None
    for line in body.splitlines():
        match = _HEAD.search(line)
        if not match:
            continue
        title, action = match.group(2).strip(), None
        for known in _ACTIONS:
            if title.endswith(" · " + known):
                title, action = title[:-len(" · " + known)].strip(), known
                break
        return match.group(1), title, action
    return None


def findings(threads):
    """The bot's inline findings among a PR's review threads, with the thread's record."""
    rows = []
    for thread in threads or []:
        comments = (thread.get("comments") or {}).get("nodes") or []
        if not comments:
            continue
        first = comments[0]
        parsed = parse_comment(first.get("body"))
        if not parsed:
            continue
        priority, title, action = parsed
        rows.append({
            "path": thread.get("path"), "line": thread.get("originalLine") or thread.get("line"),
            "priority": priority, "title": title, "action": action,
            "postedAt": first.get("createdAt"), "url": first.get("url"),
            "commit": ((first.get("originalCommit") or {}).get("oid") or "")[:12],
            "codeChanged": bool(thread.get("isOutdated")), "resolved": bool(thread.get("isResolved")),
            "body": first.get("body") or "",
            "replies": [{"by": (c.get("author") or {}).get("login") or "?", "body": c.get("body") or ""}
                        for c in comments[1:] if MARKER not in (c.get("body") or "")],
        })
    return rows


def mechanical(row):
    """The outcome the thread's flags alone support, used when the model cannot be asked.

    A changed line with no answer is still a fix; an answer needs reading, so it stays unknown.
    """
    if row.get("replies"):
        return "unknown"
    return "fixed" if row.get("codeChanged") else "unanswered"


def classify(repo, number, state, rows, model="haiku", timeout=180):
    """Ask the model what each finding came to. Returns (rows with outcome, cost)."""
    if not rows:
        return rows, 0.0
    items = []
    for index, row in enumerate(rows):
        replies = "\n".join("  - %s: %s" % (r["by"], re.sub(r"\s+", " ", r["body"])[:1200])
                            for r in row["replies"][:8]) or "  (없음)"
        body = re.sub(r"\s+", " ", row["body"].replace(MARKER, ""))[:1500]
        items.append("## %d. [%s] `%s:%s`\n지적: %s\ncodeChanged: %s · resolved: %s\nreplies:\n%s" % (
            index, row["priority"], row["path"], row["line"], body,
            str(row["codeChanged"]).lower(), str(row["resolved"]).lower(), replies))
    with open(PROMPT, encoding="utf-8") as stream:
        prompt = stream.read().format(repo=repo, number=number, state=state, items="\n\n".join(items))
    cost = 0.0
    try:
        out = reviewer._run(prompt, reviewer.ROOT, model, 1, timeout, "outcome", tools="", key="outcomes")
        cost = float((out.get("_meta") or {}).get("cost_usd") or 0.0)
        answers = {a.get("id"): a for a in out.get("outcomes") or [] if isinstance(a, dict)}
    except (reviewer.ReviewError, OSError) as exc:
        cost = getattr(exc, "cost", 0.0) or 0.0
        answers = {}
    for index, row in enumerate(rows):
        answer = answers.get(index) or {}
        if answer.get("outcome") in OUTCOMES:
            row.update(outcome=answer["outcome"], why=str(answer.get("why") or "")[:300], judged="model")
        else:
            row.update(outcome=mechanical(row), why="", judged="flags")
    return rows, cost


def _attribution(repo, number, rows, verify_log=None):
    """Which reviewer raised each finding and how the verifier scored it (verify.jsonl)."""
    seen = {}
    for entry in store.read_verify() if verify_log is None else verify_log:
        if entry.get("repo") == repo and entry.get("number") == number and entry.get("fate") == "inline":
            seen[(entry.get("path"), (entry.get("title") or "").strip())] = entry
    for row in rows:
        entry = seen.get((row["path"], row["title"]))
        if entry:
            row["agent"] = entry.get("agent")
            if isinstance(entry.get("confidence"), (int, float)):
                row["confidence"] = entry["confidence"]


def collect(repo, number, account=None, model="haiku", verify_log=None):
    """One PR's record, or None while it is still open. Returns (record, cost)."""
    data = gh.review_threads(repo, number, account=account)
    if data.get("state") not in ("MERGED", "CLOSED"):
        return None, 0.0
    rows = findings(data.get("threads"))
    rows, cost = classify(repo, number, data["state"], rows, model=model)
    _attribution(repo, number, rows, verify_log)
    for row in rows:
        row.pop("body", None)
        row["replies"] = len(row.pop("replies"))
    return {"repo": repo, "number": number, "state": data["state"],
            "closedAt": data.get("mergedAt") or data.get("closedAt"),
            "collectedAt": time.strftime("%Y-%m-%dT%H:%M:%S"), "findings": rows}, cost


def refresh(cfg, log=lambda message: None, limit=None, recheck_hours=6):
    """Collect outcomes for reviewed PRs that have since been merged or closed.

    A closed PR is collected once and never asked about again. An open one is looked at
    again only after `recheck_hours`, and `limit` bounds how many are looked at per call,
    so a tick spends a few cheap queries on this and no more.
    """
    settings = cfg.get("outcomes") or {}
    if not settings.get("enabled", True):
        return {"collected": 0, "open": 0, "cost_usd": 0.0}
    os.makedirs(store.STATE_DIR, exist_ok=True)
    with open(path() + ".lock", "a+") as guard:
        try:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"collected": 0, "open": 0, "cost_usd": 0.0, "busy": True}
        saved = load()
        now = time.time()
        waiting = []
        for key, record in store.load().items():
            if not record.get("posted") or record.get("discarded"):
                continue
            known = saved.get(key) or {}
            if known.get("state") in ("MERGED", "CLOSED"):
                continue
            if now - (known.get("checked") or 0) < recheck_hours * 3600:
                continue
            waiting.append((known.get("checked") or 0, key))
        waiting.sort()
        collected = still_open = 0
        cost = 0.0
        verify_log = store.read_verify() if waiting else []
        for _, key in waiting[:limit] if limit else waiting:
            repo, _, number = key.rpartition("#")
            entry = cfg_mod.repo_entry(cfg, repo) or {}
            account = entry.get("account") or cfg.get("account")
            try:
                record, spent = collect(repo, int(number), account=account,
                                        model=settings.get("model") or "haiku", verify_log=verify_log)
            except (gh.GhError, ValueError) as exc:
                log("  리뷰 결과 조회 실패 %s — %s" % (key, str(exc)[:100]))
                continue
            cost += spent
            if record is None:
                saved[key] = {"state": "OPEN", "checked": now}
                still_open += 1
            else:
                saved[key] = record
                collected += 1
            _save(saved)
        return {"collected": collected, "open": still_open, "cost_usd": round(cost, 4)}


def _rate(rows):
    fixed = sum(1 for r in rows if r.get("outcome") == "fixed")
    return fixed, len(rows)


def summarize(saved, since_days=None):
    """Resolution rate over merged PRs: overall and by what could explain a difference."""
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - since_days * 86400)) \
        if since_days else ""
    rows, prs = [], 0
    for record in saved.values():
        if record.get("state") != "MERGED" or (record.get("closedAt") or "") < cutoff:
            continue
        prs += 1
        rows += record.get("findings") or []

    def groups(key, order=None):
        buckets = {}
        for row in rows:
            buckets.setdefault(key(row), []).append(row)
        names = [n for n in (order or sorted(buckets, key=str)) if n in buckets]
        return [{"name": name, "fixed": _rate(buckets[name])[0], "total": len(buckets[name])} for name in names]

    def band(row):
        score = row.get("confidence")
        if not isinstance(score, (int, float)):
            return None
        return next("%d-%d" % (low, high) for low, high in ((0, 50), (51, 75), (76, 90), (91, 100))
                    if low <= score <= high)

    fixed, total = _rate(rows)
    return {
        "mergedPRs": prs, "findings": total, "fixed": fixed,
        "rate": round(fixed / total, 3) if total else None,
        "outcomes": {name: sum(1 for r in rows if r.get("outcome") == name)
                     for name in OUTCOMES + ("unknown",)},
        "codeChanged": sum(1 for r in rows if r.get("codeChanged")),
        "byPriority": groups(lambda r: r.get("priority"), ["P1", "P2", "P3", "altitude"]),
        "byAction": groups(lambda r: r.get("action") or "(표시 없음)", list(_ACTIONS) + ["(표시 없음)"]),
        "byAgent": groups(lambda r: r.get("agent") or "(기록 없음)"),
        "byConfidence": [g for g in groups(band, ["0-50", "51-75", "76-90", "91-100"])],
        "byMonth": groups(lambda r: (r.get("postedAt") or "")[:7]),
        "open": sum(1 for r in saved.values() if r.get("state") == "OPEN"),
        "closedUnmerged": sum(1 for r in saved.values() if r.get("state") == "CLOSED"),
    }

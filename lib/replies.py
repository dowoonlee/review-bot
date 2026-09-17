"""틱의 리뷰 답변 알림. status 조회는 기준선이나 알림 기록을 건드리지 않는다."""
import json
import os

from . import notify, store

LABELS = {"APPROVED": "승인", "CHANGES_REQUESTED": "변경 요청", "COMMENTED": "코멘트"}


def check(prs, account, settings, log):
    """계정별 첫 성공 조회는 기준선. 이후 PR 별 새 최신 답변을 한 알림으로 묶는다.

    리뷰 ID 로 판별하므로 같은 사람이 같은 판정을 다시 제출해도 새 답변이다.
    전송 실패는 읽음 처리하지 않아 다음 틱에 재시도한다. 알림을 껐을 때는
    읽음 처리해서 다시 켰을 때 그동안의 답변이 한꺼번에 울리지 않게 한다.
    """
    path = os.path.join(store.STATE_DIR, "review-responses.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {}
    # 손상된 파일을 빈 기준선으로 덮어쓰지 않는다. 호출자가 오류를 기록한다.
    if not isinstance(data, dict) or any(
            not isinstance(ids, list) or any(not isinstance(i, str) for i in ids)
            for ids in data.values()):
        raise ValueError("리뷰 답변 기록 형식이 잘못되었습니다")

    first = account not in data
    seen = set(data.get(account, []))
    new_count = 0
    for pr in prs:
        fresh = []
        for response in pr.get("responses", []):
            if (response["author"].lower() == pr["author"].lower()
                    or response["id"] in seen):
                continue
            fresh.append(response)
        if not fresh:
            continue
        if first or not settings.get("enabled", True):
            seen.update(r["id"] for r in fresh)
            continue
        label = "%s#%s" % (pr["repo"], pr["number"])
        summary = " · ".join("%s %s" % (r["author"], LABELS[r["state"]]) for r in fresh)
        sent = notify.send("review-bot · 새 리뷰 답변", "%s · %s\n%s" % (label, summary, pr["title"]),
                           sound=settings.get("sound", "Glass"))
        if sent:
            seen.update(r["id"] for r in fresh)
            new_count += len(fresh)
            log("리뷰 답변 알림: %s · %s" % (label, summary))
        else:
            log("리뷰 답변 알림 전송 실패: %s — 다음 틱에 재시도합니다" % label)

    data[account] = sorted(seen)
    os.makedirs(store.STATE_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    if first:
        log("리뷰 답변 기준선 저장 (%s): %d건 — 다음 틱부터 새 답변을 알립니다" % (account, len(seen)))
    return new_count

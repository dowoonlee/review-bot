"""gh CLI 래퍼. 계정을 고정할 수 있고, 쓰기는 post_* 두 곳으로만 나간다.

계정 고정은 GH_TOKEN 을 자식 프로세스 env 로만 넘겨서 한다. `gh auth switch` 는
전역 상태라 사용자의 터미널 세션까지 바꿔 버리고, GH_CONFIG_DIR 분리는 키체인의
활성 슬롯을 그대로 읽어 계정을 고르지 못한다. 토큰은 키체인에서 읽어 프로세스
메모리와 자식 env 에만 머물며, 파일·로그·명령 인자로는 절대 나가지 않는다.
"""
import json
import os
import re
import subprocess
import time

HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")

_TOKENS = {}     # account → token. 프로세스 수명 동안만 산다.
_LOGINS = {}     # account → 토큰의 실제 소유자 login


class GhError(Exception):
    pass


def _token(account):
    if account in _TOKENS:
        return _TOKENS[account]
    try:
        p = subprocess.run(["gh", "auth", "token", "--user", account],
                           capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as e:
        raise GhError("gh auth token 실패: %s" % e)
    tok = (p.stdout or "").strip()
    if p.returncode != 0 or not tok:
        raise GhError("계정 %s 의 토큰을 키체인에서 찾지 못했습니다: %s"
                      % (account, (p.stderr or "").strip()[:200]))
    _TOKENS[account] = tok
    return tok


def env_for(account):
    """계정이 고정됐으면 GH_TOKEN 을 실은 env, 아니면 None (= 사용자 전역 gh 그대로)."""
    if not account:
        return None
    env = dict(os.environ)
    env["GH_TOKEN"] = _token(account)
    env.pop("GITHUB_TOKEN", None)
    return env


def _gh(args, timeout=120, account=None):
    try:
        p = subprocess.run(["gh"] + args, capture_output=True, text=True,
                           timeout=timeout, env=env_for(account))
    except subprocess.TimeoutExpired:
        raise GhError("gh 시간 초과: %s" % " ".join(args[:3]))
    except OSError as e:
        raise GhError("gh 실행 실패: %s" % e)
    if p.returncode != 0:
        raise GhError((p.stderr or p.stdout or "").strip()[:400])
    return p.stdout


def _gh_json(args, timeout=120, account=None):
    out = _gh(args, timeout, account)
    try:
        return json.loads(out or "null")
    except ValueError as e:
        raise GhError("gh 응답이 JSON 이 아닙니다: %s" % e)


def whoami(account=None):
    """이 계정(또는 전역 활성 gh)으로 API 를 부르면 실제로 누구인가.

    설정에 적힌 이름이 아니라 토큰이 말하는 이름이다. 둘이 다르면 잘못된 이름으로
    게시하게 되므로, 호출자는 이 값을 설정과 대조해야 한다.
    """
    if account and account in _LOGINS:
        return _LOGINS[account]
    login = (_gh_json(["api", "user"], timeout=30, account=account) or {}).get("login")
    if account:
        _LOGINS[account] = login
    return login


def active_account():
    """사용자 전역 gh 의 활성 계정 이름. 계정을 고정하지 않았을 때만 의미가 있다."""
    try:
        out = subprocess.run(["gh", "auth", "status"], capture_output=True,
                             text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    acct = None
    for line in out.splitlines():
        m = re.search(r"Logged in to \S+ account (\S+)", line)
        if m:
            acct = m.group(1)
        if "Active account: true" in line and acct:
            return acct
    return acct


def review_requests(limit=30, account=None, attempts=3):
    """나에게 리뷰 요청이 온 열린 PR 목록.

    맥이 잠에서 깨는 순간 틱이 뜨면 Wi-Fi 가 아직 붙지 않아 이 조회만 죽는다. 읽기라
    다시 물어도 안전하므로, 한 번에 오래 매달리는 대신 짧게 여러 번 시도한다.
    """
    data = None
    for i in range(attempts):
        try:
            data = _gh_json([
                "search", "prs", "--review-requested=@me", "--state=open",
                "--limit", str(limit),
                "--json", "repository,number,title,url,author,updatedAt,isDraft",
            ], timeout=45, account=account)
            break
        except GhError:
            if i + 1 >= attempts:
                raise
            time.sleep(5 * (i + 1))
    out = []
    for r in data or []:
        out.append({
            "repo": (r.get("repository") or {}).get("nameWithOwner"),
            "number": r.get("number"),
            "title": r.get("title") or "",
            "url": r.get("url") or "",
            "author": (r.get("author") or {}).get("login") or "",
            "updatedAt": r.get("updatedAt") or "",
            "isDraft": bool(r.get("isDraft")),
        })
    return [r for r in out if r["repo"] and r["number"]]


_SENT_REQUESTS_QUERY = """
query($searchQuery: String!, $cursor: String) {
  search(query: $searchQuery, type: ISSUE, first: 25, after: $cursor) {
    issueCount
    pageInfo { hasNextPage endCursor }
    nodes { ... on PullRequest {
      number title url updatedAt isDraft reviewDecision mergeable headRefName
      repository { nameWithOwner }
      author { login }
      reviewRequests(first: 100) {
        pageInfo { hasNextPage }
        nodes { requestedReviewer {
          ... on User { login }
          ... on Team { slug organization { login } }
          ... on Mannequin { login }
        } }
      }
      latestReviews(first: 100) {
        pageInfo { hasNextPage }
        nodes { id author { login } state submittedAt }
      }
      timelineItems(last: 1, itemTypes: [REVIEW_REQUESTED_EVENT]) {
        nodes { ... on ReviewRequestedEvent { createdAt } }
      }
      commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
    } }
  }
}
"""


def sent_review_requests(account=None):
    """내가 작성한 열린 PR 중 리뷰 요청 이력이 있는 것. 리뷰가 끝나도 유지한다.

    검색은 페이지를 끝까지 읽는다. 상세 연결의 한도 초과는 조용히 잘라내지 않고
    경고한다. 화면과 틱의 답변 알림에서 쓰며 자동 리뷰 대상에는 들어가지 않는다.
    """
    login = whoami(account)
    if not login:
        raise GhError("현재 계정을 확인할 수 없습니다")
    cursor, out, warnings = None, [], []
    while True:
        args = ["api", "graphql", "-f", "query=" + _SENT_REQUESTS_QUERY,
                "-f", "searchQuery=is:pr is:open author:%s sort:updated-desc" % login]
        if cursor:
            args += ["-f", "cursor=" + cursor]
        payload = _gh_json(args, timeout=60, account=account)
        if payload.get("errors"):
            raise GhError("보낸 요청 GraphQL 조회 실패")
        search = (payload.get("data") or {}).get("search")
        if search is None:
            raise GhError("보낸 요청 검색 결과가 없습니다")
        for pr in search.get("nodes") or []:
            if not pr or not pr.get("number"):
                continue
            requests = pr.get("reviewRequests") or {}
            history = (pr.get("timelineItems") or {}).get("nodes") or []
            if not requests.get("nodes") and not history:
                continue
            reviews = pr.get("latestReviews") or {}
            reviewers = {}
            for review in reviews.get("nodes") or []:
                name = (review.get("author") or {}).get("login")
                if name:
                    reviewers[name.lower()] = {"name": name, "state": review["state"]}
            for request in requests.get("nodes") or []:
                target = request.get("requestedReviewer") or {}
                name = target.get("login") or (
                    "%s/%s" % ((target.get("organization") or {}).get("login"), target["slug"])
                    if target.get("slug") else None)
                if name:
                    reviewers[name.lower()] = {"name": name, "state": "PENDING"}
            repo = pr["repository"]["nameWithOwner"]
            incomplete = any(c.get("pageInfo", {}).get("hasNextPage")
                             for c in (requests, reviews))
            if incomplete:
                warnings.append("%s#%s: 리뷰어가 많아 일부만 표시합니다" % (repo, pr["number"]))
            commits = (pr.get("commits") or {}).get("nodes") or []
            checks = ((commits[-1].get("commit") or {}).get("statusCheckRollup") or {}) if commits else {}
            out.append({
                "repo": repo, "number": pr["number"], "title": pr["title"],
                "url": pr["url"], "author": (pr.get("author") or {}).get("login") or login,
                "account": account or "global", "updatedAt": pr["updatedAt"],
                "isDraft": pr["isDraft"], "reviewDecision": pr.get("reviewDecision"),
                "mergeable": pr.get("mergeable"), "checksState": checks.get("state"),
                "headRefName": pr.get("headRefName"),
                "requestedAt": history[-1].get("createdAt") if history else None,
                # 재요청으로 표시 상태가 PENDING 이 되어도 제출된 답변은 따로 보존한다.
                "responses": [{"id": r["id"], "author": r["author"]["login"],
                               "state": r["state"], "submittedAt": r.get("submittedAt")}
                              for r in reviews.get("nodes") or []
                              if r.get("id") and (r.get("author") or {}).get("login")
                              and r.get("state") in ("APPROVED", "CHANGES_REQUESTED", "COMMENTED")],
                "reviewers": sorted(reviewers.values(), key=lambda r: (r["state"] != "PENDING", r["name"])),
            })
        page = search["pageInfo"]
        if not page["hasNextPage"]:
            if search.get("issueCount", 0) > 1000:
                warnings.append("보낸 요청 검색 한도(열린 PR 1,000개)를 초과했습니다")
            break
        next_cursor = page.get("endCursor")
        if not next_cursor or next_cursor == cursor:
            raise GhError("보낸 요청의 다음 페이지를 읽을 수 없습니다")
        cursor = next_cursor
    return out, warnings


def pr_meta(repo, number, account=None, timeout=120):
    return _gh_json([
        "pr", "view", str(number), "--repo", repo, "--json",
        "headRefOid,headRefName,baseRefOid,baseRefName,isDraft,additions,deletions,"
        "changedFiles,title,body,author,url,mergeable,state,updatedAt,statusCheckRollup,"
        "reviewDecision,latestReviews,reviewRequests",
    ], account=account, timeout=timeout)


def ci_state(meta):
    """head 커밋의 CI 상태를 한 단어로 — none / success / failure / pending.

    로컬 실행 결과와 구분해 저장소 CI 상태를 전달한다. 체크가 없거나 모두
    skipped/neutral이면 `none`이고, EXPECTED는 아직 통과하지 않은 상태다.
    """
    checks = (meta or {}).get("statusCheckRollup") or []
    if not checks:
        return "none"
    states = set()
    for c in checks:
        if not isinstance(c, dict):
            continue
        # CheckRun 은 status/conclusion, StatusContext 는 state 로 온다.
        concl = str(c.get("conclusion") or c.get("state") or "").upper()
        status = str(c.get("status") or "").upper()
        if concl in ("FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED",
                     "STARTUP_FAILURE"):
            states.add("failure")
        elif concl == "SUCCESS":
            states.add("success")
        elif concl in ("NEUTRAL", "SKIPPED"):
            states.add("skipped")
        elif status in ("QUEUED", "IN_PROGRESS", "PENDING", "WAITING", "REQUESTED") \
                or concl in ("PENDING", ""):
            states.add("pending")
        else:
            states.add("pending")
    if "failure" in states:
        return "failure"
    if "pending" in states:
        return "pending"
    return "success" if "success" in states else "none"


def ci_evidence(meta):
    """Keep individual check names and links, including skipped checks."""
    checks = []
    for check in (meta or {}).get("statusCheckRollup") or []:
        if not isinstance(check, dict):
            continue
        checks.append({"name": check.get("name") or check.get("context") or "unnamed",
                       "status": check.get("status") or check.get("state"),
                       "conclusion": check.get("conclusion"),
                       "url": check.get("detailsUrl") or check.get("targetUrl")})
    return {"sha": (meta or {}).get("headRefOid"), "state": ci_state(meta), "checks": checks}


def pr_files(repo, number, max_pages=30, account=None):
    """변경 파일 + patch. per_page 100 으로 페이지를 넘긴다."""
    files = []
    for page in range(1, max_pages + 1):
        chunk = _gh_json([
            "api", "repos/%s/pulls/%s/files?per_page=100&page=%d"
            % (repo, number, page),
        ], account=account)
        if not chunk:
            break
        files.extend(chunk)
        if len(chunk) < 100:
            break
    return files


def postable_lines(patch):
    """patch 에서 RIGHT side 로 코멘트 가능한 새 파일 라인 번호 집합.

    GitHub 은 diff hunk 에 나타난 라인에만 inline comment 를 받는다. 추가(+)와
    context( ) 라인이 대상이고, 삭제(-)는 RIGHT side 에 존재하지 않는다.
    """
    lines = set()
    if not patch:
        return lines
    new_ln = None
    for raw in patch.split("\n"):
        m = HUNK_RE.match(raw)
        if m:
            new_ln = int(m.group(1))
            continue
        if new_ln is None:
            continue
        if raw.startswith("+"):
            lines.add(new_ln)
            new_ln += 1
        elif raw.startswith("-"):
            continue
        elif raw.startswith("\\"):   # "\ No newline at end of file"
            continue
        else:                        # context
            lines.add(new_ln)
            new_ln += 1
    return lines


def added_lines(patch):
    """추가(+)된 라인만. 리뷰 우선순위를 매길 때 쓴다."""
    lines = set()
    if not patch:
        return lines
    new_ln = None
    for raw in patch.split("\n"):
        m = HUNK_RE.match(raw)
        if m:
            new_ln = int(m.group(1))
            continue
        if new_ln is None:
            continue
        if raw.startswith("+"):
            lines.add(new_ln)
            new_ln += 1
        elif raw.startswith("-") or raw.startswith("\\"):
            continue
        else:
            new_ln += 1
    return lines


def existing_bot_comments(repo, number, marker, account=None):
    """이미 봇이 단 inline 코멘트의 (path, line, 본문 앞머리) 지문."""
    try:
        data = _gh_json(["api",
                         "repos/%s/pulls/%s/comments?per_page=100" % (repo, number)],
                        account=account)
    except GhError:
        return set()
    out = set()
    for c in data or []:
        body = c.get("body") or ""
        if marker and marker not in body:
            continue
        out.add((c.get("path"), c.get("line") or c.get("original_line"),
                 body.split("\n")[0][:80]))
    return out


def review_decision(repo, number, account=None):
    """PR 의 유효 판정. 비어 있으면 아무도 차단하지 않은 것이다."""
    d = _gh_json(["pr", "view", str(number), "--repo", repo,
                  "--json", "reviewDecision"], account=account)
    return (d or {}).get("reviewDecision") or ""


def lost_anchors(repo, number, login, marker, account=None):
    """내가 단 봇 코멘트 중 line 이 비어 diff 밖으로 떨어진 것의 수."""
    data = _gh_json(["api",
                     "repos/%s/pulls/%s/comments?per_page=100" % (repo, number)],
                    account=account)
    n = 0
    for c in data or []:
        if login and (c.get("user") or {}).get("login") != login:
            continue
        if marker and marker not in (c.get("body") or ""):
            continue
        if c.get("line") is None:
            n += 1
    return n


EVENTS = ("COMMENT", "APPROVE", "REQUEST_CHANGES")


def _post(path, payload, account=None):
    try:
        p = subprocess.run(
            ["gh", "api", "--method", "POST", path, "--input", "-"],
            input=json.dumps(payload), capture_output=True, text=True,
            timeout=120, env=env_for(account))
    except subprocess.TimeoutExpired:
        raise GhError("gh 시간 초과: POST %s" % path)
    except OSError as e:
        raise GhError("gh 실행 실패: %s" % e)
    if p.returncode != 0:
        raise GhError((p.stderr or p.stdout or "").strip()[:600])
    return json.loads(p.stdout or "{}")


def post_review(repo, number, commit_sha, body, comments, event="COMMENT",
                account=None):
    """리뷰 하나를 남긴다. event 는 판정 단계가 정한다.

    inline 좌표가 하나라도 틀리면 GitHub 이 422 로 전체를 거절하고, 자기 PR 에
    APPROVE 를 내도 422 다. 실패하면 호출자가 단계적으로 강등할 수 있도록 예외를
    그대로 올린다.
    """
    if event not in EVENTS:
        event = "COMMENT"
    payload = {"commit_id": commit_sha, "event": event, "body": body}
    if comments:
        # 초안에는 diff 문맥 같은 부가 정보가 붙는다. GitHub 이 모르는 키가 하나라도
        # 섞이면 422 로 전체가 거절되므로, 나갈 때 허용된 키만 남긴다.
        allowed = ("path", "line", "side", "start_line", "start_side", "body")
        payload["comments"] = [{k: c[k] for k in allowed if k in c} for c in comments]
    return _post("repos/%s/pulls/%s/reviews" % (repo, number), payload, account)


def post_issue_comment(repo, number, body, account=None):
    return _post("repos/%s/issues/%s/comments" % (repo, number),
                 {"body": body}, account)


def hunk_around(patch, line, context=4):
    """인라인 코멘트가 가리키는 줄의 앞뒤를 git diff 모양 그대로 잘라 준다.

    돌려주는 각 줄은 {"old", "new", "sign", "text"} 이고 sign 은 diff 그대로(" +-")다.
    line 은 새 파일 기준 줄 번호 — 인라인 코멘트가 쓰는 좌표와 같다.
    """
    if not patch or not line:
        return []
    rows, old_no, new_no = [], None, None
    for raw in patch.split("\n"):
        m = HUNK_RE.match(raw)
        if m:
            new_no = int(m.group(1))
            m2 = re.match(r"^@@ -(\d+)", raw)
            old_no = int(m2.group(1)) if m2 else None
            continue
        if new_no is None:
            continue
        if raw.startswith("\\"):                     # "\ No newline at end of file"
            continue
        if raw.startswith("+"):
            rows.append({"old": None, "new": new_no, "sign": "+", "text": raw[1:]})
            new_no += 1
        elif raw.startswith("-"):
            rows.append({"old": old_no, "new": None, "sign": "-", "text": raw[1:]})
            old_no = (old_no or 0) + 1
        else:
            rows.append({"old": old_no, "new": new_no, "sign": " ",
                         "text": raw[1:] if raw[:1] == " " else raw})
            new_no += 1
            old_no = (old_no or 0) + 1

    idx = next((i for i, r in enumerate(rows) if r["new"] == line), None)
    if idx is None:
        return []
    return rows[max(0, idx - context):idx + context + 1]


_LAST_REQUEST_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      timelineItems(last: 50, itemTypes: [REVIEW_REQUESTED_EVENT]) {
        nodes { ... on ReviewRequestedEvent {
          createdAt
          actor { login }
          requestedReviewer {
            ... on User { login }
            ... on Team { slug }
            ... on Mannequin { login }
          }
        } }
      }
    }
  }
}
"""


def last_review_request(repo, number, login, account=None, timeout=60):
    """나에게 리뷰를 요청한 **마지막** 사건 — `{"at": ISO8601(UTC), "by": 로그인}`.

    리뷰 요청은 PR 본문이 아니라 타임라인에만 남는다. 같은 커밋 그대로 리뷰를 다시
    요청하는 일이 흔해서, 봇은 "이 요청에 이미 답했는가" 를 이 시각으로 가른다 —
    커밋 지문만 보면 재요청이 영원히 보이지 않는다.

    REST 타임라인이 아니라 GraphQL 로 묻는 것은 `last:` 로 최근 것부터 받기
    때문이다. 코멘트가 많은 PR 에서 REST 첫 페이지만 읽으면 정작 최근 요청을
    놓친다. 요청 이력이 없으면 `None`.
    """
    if not login:
        return None
    owner, _, name = (repo or "").partition("/")
    if not owner or not name:
        return None
    payload = _gh_json(["api", "graphql",
                        "-f", "query=" + _LAST_REQUEST_QUERY,
                        "-f", "owner=" + owner, "-f", "name=" + name,
                        "-F", "number=%d" % int(number)],
                       timeout=timeout, account=account)
    if payload.get("errors"):
        raise GhError("리뷰 요청 이력 GraphQL 조회 실패 (%s#%s)" % (repo, number))
    pr = (((payload.get("data") or {}).get("repository") or {}).get("pullRequest") or {})
    last = None
    for e in (pr.get("timelineItems") or {}).get("nodes") or []:
        target = e.get("requestedReviewer") or {}
        name_of = target.get("login") or target.get("slug") or ""
        if name_of.lower() != login.lower():
            continue
        last = {"at": e.get("createdAt") or "",
                "by": (e.get("actor") or {}).get("login") or ""}
    return last


def review_requester(repo, number, login, account=None):
    """나에게 리뷰를 요청한 사람. 타임라인에만 남는 사실이라 따로 물어본다."""
    try:
        last = last_review_request(repo, number, login, account=account)
    except GhError:
        return None
    return (last or {}).get("by") or None


ISSUE_REF_RE = re.compile(r"#(\d+)")


def linked_issues(repo, number, title="", body="", account=None, limit=3, body_chars=0):
    """PR 이 가리키는 이슈들 — 제목·본문의 #번호 를 따라간다.

    GitHub 의 자동 연결(Closes #N)만 보면 규칙대로 안 쓴 PR 을 놓치므로, 눈에 보이는
    참조를 그대로 따라간다. PR 자신과 중복은 뺀다. `body_chars` 를 주면 이슈 본문도
    그 길이까지 싣는다 — 리뷰어가 이 PR 이 풀려는 문제를 읽을 수 있게.
    """
    seen, out = set(), []
    for m in ISSUE_REF_RE.finditer("%s\n%s" % (title or "", body or "")):
        n = int(m.group(1))
        if n == number or n in seen:
            continue
        seen.add(n)
        if len(out) >= limit:
            break
        try:
            d = _gh_json(["api", "repos/%s/issues/%s" % (repo, n)],
                         timeout=60, account=account)
        except GhError:
            continue
        if not d or d.get("pull_request"):      # 다른 PR 을 가리킨 것은 이슈가 아니다
            continue
        issue = {"number": n, "title": d.get("title") or "",
                 "author": ((d.get("user") or {}).get("login") or ""),
                 "state": d.get("state") or "", "url": d.get("html_url") or ""}
        if body_chars:
            # 본문에 스쳐 지나간 #번호까지 요구사항으로 읽히면 안 된다. 제목이 가리키거나
            # 닫는다고 밝힌 이슈만 이 PR 이 푸는 문제이고, 나머지는 언급일 뿐이라 짧게 싣는다.
            solves = (re.search(r"#%d\b" % n, title or "") or
                      re.search(r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s+#%d\b" % n, body or ""))
            issue["solves"] = bool(solves)
            keep = body_chars if solves else min(body_chars, 500)
            text = (d.get("body") or "").strip()
            issue["body"] = text[:keep] + ("\n… (이하 생략)" if len(text) > keep else "")
        out.append(issue)
    return out


def file_review_comments(repo, paths, account=None, per_file=3, max_files=10):
    """이 PR 이 건드린 파일에 **과거 PR 에서** 달렸던 리뷰 코멘트 — {경로: [줄]}.

    같은 파일에서 예전에 지적된 것이 이번에 또 반복되는지 보라고 주는 자료다.
    파일별로 조회하는 API 가 없어 저장소의 최근 코멘트를 한 번에 받아 걸러낸다 —
    커버리지는 최근 100건까지지만 호출 한 번으로 끝난다.
    """
    want = {p for p in list(paths)[:max_files]}
    if not want:
        return {}
    try:
        data = _gh_json(["api", "repos/%s/pulls/comments?per_page=100"
                                "&sort=created&direction=desc" % repo],
                        account=account)
    except GhError:
        return {}             # 맥락은 있으면 좋은 것이지 리뷰를 막을 이유가 아니다
    out = {}
    for c in data or []:
        path = c.get("path")
        if path not in want or len(out.get(path) or []) >= per_file:
            continue
        who = ((c.get("user") or {}).get("login")) or "?"
        body = " ".join((c.get("body") or "").split())[:200]
        if not body:
            continue
        url = c.get("html_url") or ""
        pr = url.rpartition("/pull/")[2].partition("#")[0]
        out.setdefault(path, []).append(
            "%s (PR #%s): %s" % (who, pr or "?", body))
    return out


_REQUEST_CONTEXT = '''
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      title body url headRefOid baseRefOid isDraft reviewDecision mergeable
      author { login }
      commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
      reviews(last: 50) {
        pageInfo { hasPreviousPage }
        nodes { id body url state submittedAt author { login } commit { oid } }
      }
      comments(last: 30) {
        pageInfo { hasPreviousPage }
        nodes { id body url updatedAt author { login } }
      }
      reviewThreads(last: 50) {
        pageInfo { hasPreviousPage }
        nodes { id isResolved isOutdated path line comments(last: 20) {
          pageInfo { hasPreviousPage }
          nodes { id body url updatedAt author { login } originalCommit { oid } }
        } }
      }
    }
  }
}
'''


def request_context(repo, number, account=None, bounded=True):
    """Bounded conversation snapshot, including edits/replies/resolution in the digest.

    Incomplete connections/text are explicitly marked; no inference that unseen feedback is done.
    """
    owner, name = repo.split('/', 1)
    payload = _gh_json(['api', 'graphql', '-f', 'query=' + _REQUEST_CONTEXT,
                       '-f', 'owner=' + owner, '-f', 'name=' + name,
                       '-F', 'number=%d' % number], timeout=30, account=account)
    if payload.get('errors'):
        raise GhError('리뷰 대화 조회 실패')
    pr = ((payload.get('data') or {}).get('repository') or {}).get('pullRequest')
    if not pr:
        raise GhError('PR 대화를 읽지 못했습니다')
    incomplete = False
    remaining = 45000
    def clip(text):
        nonlocal incomplete, remaining
        text = text or ''
        clipped = text[:min(4000, remaining)]
        if len(clipped) != len(text): incomplete = True
        remaining -= len(clipped)
        return clipped
    sources = []
    sources.append({'id': 'pr', 'url': pr['url'], 'body': pr.get('body'),
                    'author': (pr.get('author') or {}).get('login')})
    for field in ('reviews', 'comments', 'reviewThreads'):
        connection = pr.get(field) or {}
        if (connection.get('pageInfo') or {}).get('hasPreviousPage'): incomplete = True
        for node in connection.get('nodes') or []:
            if field == 'reviewThreads':
                comments = node.get('comments') or {}
                if (comments.get('pageInfo') or {}).get('hasPreviousPage'): incomplete = True
                rows = comments.get('nodes') or []
            else:
                rows = [node]
            for comment in rows:
                sources.append({'id': comment['id'], 'url': comment['url'],
                    'body': comment.get('body'), 'author': (comment.get('author') or {}).get('login'),
                    'state': comment.get('state'), 'at': comment.get('updatedAt') or comment.get('submittedAt'),
                    'commit': (comment.get('commit') or comment.get('originalCommit') or {}).get('oid'),
                    'thread': node['id'] if field == 'reviewThreads' else None,
                    'resolved': bool(node.get('isResolved')), 'outdated': bool(node.get('isOutdated')),
                    'path': node.get('path'), 'line': node.get('line')})
    if bounded and len(sources) > 100:
        incomplete = True
        sources = sources[:1] + sources[-99:]
    for source in sources:
        source['body'] = clip(source.get('body')) if bounded else (source.get('body') or '')
    commits = (pr.get('commits') or {}).get('nodes') or []
    checks = ((commits[-1].get('commit') or {}).get('statusCheckRollup') or {}).get('state') if commits else None
    return {'repo': repo, 'number': number, 'title': pr['title'], 'head': pr['headRefOid'],
            'base': pr['baseRefOid'], 'isDraft': pr['isDraft'], 'reviewDecision': pr.get('reviewDecision'),
            'mergeable': pr.get('mergeable'), 'checksState': checks,
            'sources': sources, 'incomplete': incomplete}


_ISSUE_FIELDS = '''
      issueFieldValues(first: 10) { nodes {
        ... on IssueFieldSingleSelectValue { optionId field { ... on IssueFieldSingleSelect { name options { id name } } } }
        ... on IssueFieldDateValue { value field { ... on IssueFieldDate { name } } }
      } }
'''

_ASSIGNED_ISSUES_QUERY = '''
query($searchQuery: String!, $cursor: String) {
  search(query: $searchQuery, type: ISSUE, first: 50, after: $cursor) {
    issueCount
    pageInfo { hasNextPage endCursor }
    nodes { ... on Issue {
      number title url createdAt updatedAt
      repository { nameWithOwner }
      author { login }
      labels(first: 20) { nodes { name } }
      milestone { title dueOn }
      comments { totalCount }
      issueType { name }
''' + _ISSUE_FIELDS + '''
    } }
  }
}
'''

ASSIGNED_ISSUES_CAP = 200


def issue_fields(node):
    """org 이슈 필드(Priority·Target date 등)를 {이름: 값} 으로 편다. 옵션 id 는 필드가
    함께 내려 주는 옵션 표로 이름으로 바꾼다 — id 를 하드코딩하면 org 가 바뀔 때 깨진다."""
    out = {}
    for value in ((node or {}).get('issueFieldValues') or {}).get('nodes') or []:
        field = (value or {}).get('field') or {}
        name = field.get('name')
        if not name:
            continue
        if 'optionId' in value:
            options = {o.get('id'): o.get('name') for o in field.get('options') or []}
            out[name] = options.get(value.get('optionId'))
        elif 'value' in value:
            out[name] = value.get('value')
    return out


def _issue_row(node, account):
    fields = issue_fields(node)
    milestone = node.get('milestone') or {}
    return {
        'repo': node['repository']['nameWithOwner'], 'number': node['number'],
        'title': node.get('title') or '', 'url': node.get('url') or '',
        'author': (node.get('author') or {}).get('login') or '',
        'account': account or 'global',
        'createdAt': node.get('createdAt') or '', 'updatedAt': node.get('updatedAt') or '',
        'labels': [l.get('name') for l in (node.get('labels') or {}).get('nodes') or [] if l.get('name')],
        'milestone': milestone.get('title'), 'milestoneDue': milestone.get('dueOn'),
        'comments': (node.get('comments') or {}).get('totalCount') or 0,
        'type': (node.get('issueType') or {}).get('name'),
        'priority': fields.get('Priority'),
        'targetDate': fields.get('Target date'),
    }


def assigned_issues(account=None):
    """나에게 할당된 열린 이슈. PR 은 뺀다 (is:issue). 우선순위는 org 이슈 필드에서 읽는다.

    페이지는 상한(ASSIGNED_ISSUES_CAP)까지 읽고, 넘치면 조용히 자르지 않고 경고한다.
    """
    cursor, out, warnings = None, [], []
    while True:
        args = ['api', 'graphql', '-f', 'query=' + _ASSIGNED_ISSUES_QUERY,
                '-f', 'searchQuery=is:issue is:open assignee:@me sort:updated-desc']
        if cursor:
            args += ['-f', 'cursor=' + cursor]
        payload = _gh_json(args, timeout=60, account=account)
        if payload.get('errors'):
            raise GhError('담당 이슈 GraphQL 조회 실패')
        search = (payload.get('data') or {}).get('search')
        if search is None:
            raise GhError('담당 이슈 검색 결과가 없습니다')
        for node in search.get('nodes') or []:
            if node and node.get('number') and (node.get('repository') or {}).get('nameWithOwner'):
                out.append(_issue_row(node, account))
        page = search.get('pageInfo') or {}
        if not page.get('hasNextPage'):
            break
        if len(out) >= ASSIGNED_ISSUES_CAP:
            out = out[:ASSIGNED_ISSUES_CAP]
            warnings.append('담당 이슈가 %d건을 넘어 최근 갱신 순으로 %d건만 표시합니다'
                            % (ASSIGNED_ISSUES_CAP, len(out)))
            break
        next_cursor = page.get('endCursor')
        if not next_cursor or next_cursor == cursor:
            raise GhError('담당 이슈의 다음 페이지를 읽을 수 없습니다')
        cursor = next_cursor
    return out, warnings


_ISSUE_CONTEXT = '''
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      title url body state createdAt updatedAt
      author { login }
      assignees(first: 10) { nodes { login } }
      labels(first: 20) { nodes { name } }
      milestone { title dueOn }
      issueType { name }
      comments(last: 30) {
        pageInfo { hasPreviousPage }
        nodes { id url body createdAt author { login } }
      }
''' + _ISSUE_FIELDS + '''
    }
  }
}
'''


def issue_context(repo, number, account=None):
    """Jev 에 보낼 이슈 스냅샷. 본문과 최근 댓글만 잘라 담고, 잘린 곳은 incomplete 로 표시한다."""
    owner, name = repo.split('/', 1)
    payload = _gh_json(['api', 'graphql', '-f', 'query=' + _ISSUE_CONTEXT,
                        '-f', 'owner=' + owner, '-f', 'name=' + name,
                        '-F', 'number=%d' % number], timeout=30, account=account)
    if payload.get('errors'):
        raise GhError('이슈 조회 실패')
    issue = ((payload.get('data') or {}).get('repository') or {}).get('issue')
    if not issue:
        raise GhError('이슈를 읽지 못했습니다')
    incomplete = False
    remaining = 30000

    def clip(text):
        nonlocal incomplete, remaining
        text = text or ''
        clipped = text[:min(4000, remaining)]
        if len(clipped) != len(text):
            incomplete = True
        remaining -= len(clipped)
        return clipped
    sources = [{'id': 'issue', 'url': issue['url'], 'body': clip(issue.get('body')),
                'author': (issue.get('author') or {}).get('login'), 'at': issue.get('createdAt')}]
    comments = issue.get('comments') or {}
    if (comments.get('pageInfo') or {}).get('hasPreviousPage'):
        incomplete = True
    for comment in comments.get('nodes') or []:
        sources.append({'id': comment['id'], 'url': comment['url'], 'body': clip(comment.get('body')),
                        'author': (comment.get('author') or {}).get('login'), 'at': comment.get('createdAt')})
    milestone = issue.get('milestone') or {}
    return {'repo': repo, 'number': number, 'title': issue['title'], 'state': issue.get('state'),
            'type': (issue.get('issueType') or {}).get('name'),
            'labels': [l.get('name') for l in (issue.get('labels') or {}).get('nodes') or []],
            'assignees': [a.get('login') for a in (issue.get('assignees') or {}).get('nodes') or []],
            'milestone': milestone.get('title'), 'milestoneDue': milestone.get('dueOn'),
            'fields': issue_fields(issue), 'createdAt': issue.get('createdAt'), 'updatedAt': issue.get('updatedAt'),
            'sources': sources, 'incomplete': incomplete}


_REVIEW_THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      state mergedAt closedAt headRefOid
      reviewThreads(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          isResolved isOutdated path line originalLine
          resolvedBy { login }
          comments(first: 20) {
            totalCount
            nodes { author { login } body createdAt url originalCommit { oid } }
          }
        }
      }
    }
  }
}
"""


def review_threads(repo, number, account=None, timeout=60):
    """A PR's state and every inline review thread, with whether the code under it
    changed afterwards (`isOutdated`) and whether someone resolved it.

    Read-only. Used after the fact to see what became of the bot's findings.
    """
    owner, _, name = (repo or "").partition("/")
    if not owner or not name:
        raise GhError("저장소 이름이 올바르지 않습니다: %s" % repo)
    result, cursor = None, None
    while True:
        args = ["api", "graphql", "-f", "query=" + _REVIEW_THREADS_QUERY,
                "-f", "owner=" + owner, "-f", "name=" + name, "-F", "number=%d" % int(number)]
        if cursor:
            args += ["-f", "after=" + cursor]
        payload = _gh_json(args, timeout=timeout, account=account)
        if payload.get("errors"):
            raise GhError("리뷰 스레드 GraphQL 조회 실패 (%s#%s)" % (repo, number))
        pr = (((payload.get("data") or {}).get("repository") or {}).get("pullRequest") or None)
        if pr is None:
            raise GhError("PR 을 찾지 못했습니다 (%s#%s)" % (repo, number))
        threads = pr.get("reviewThreads") or {}
        if result is None:
            result = {"state": pr.get("state"), "mergedAt": pr.get("mergedAt"),
                      "closedAt": pr.get("closedAt"), "head": pr.get("headRefOid"), "threads": []}
        result["threads"] += threads.get("nodes") or []
        page = threads.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            return result
        cursor = page.get("endCursor")

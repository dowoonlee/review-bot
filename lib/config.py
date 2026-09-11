"""설정 로드 — config.json 이 있으면 기본값 위에 덮어쓴다.

기본값은 의도적으로 보수적이다. repos 가 비어 있으면 아무 PR 도 리뷰하지 않고,
dryRun 이 켜져 있어 게시도 하지 않는다. 설정 파일 없이 실행했을 때 사내 PR 에
코멘트가 나가는 일이 없어야 한다.
"""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULTS = {
    # 봇이 조회·게시에 쓸 gh 계정. null 이면 그 순간 활성인 전역 gh 계정을 따라간다 —
    # 계정이 둘 이상이면 반드시 적어야 한다. 저장소마다 "account" 로 덮어쓸 수 있다.
    "account": None,
    # "owner/name": {"localPath": "~/dev/repo", "enabled": true, "account": "..."}
    # 여기 없는 저장소의 PR 은 건너뛴다 — 화이트리스트가 곧 안전장치다.
    "repos": {},
    "review": {
        # 배포판이 들고 오는 리뷰어는 이 하나뿐이다. 관점을 늘리려면
        # `review-bot agent add <정의.md>` 로 직접 등록한다.
        "agents": ["code-reviewer"],
        # 문자열이면 리뷰어 전부 그 모델, dict 면 에이전트별로 갈라 준다.
        # 에이전트 정의의 model 은 어느 쪽이든 무시한다 — 비용은 여기 한 곳에서만
        # 정해져야 추적이 된다.
        #
        # 기본이 sonnet 인 이유: 얕고 넓게 훑는 관점은 sonnet 으로도 같은 것을
        # 잡는다(실측). 보안만 opus 로 두는 것은, 취약점은 추론 한 단계를 더
        # 들어가야 보이고 놓쳤을 때의 대가가 다른 관점과 다르기 때문이다.
        "model": "sonnet",
        # 리뷰어 하나가 쓸 수 있는 도구 호출 횟수. 0 이면 상한 없음 — 리뷰어가
        # 스스로 끝낼 때까지 둔다. 양수를 주면 예산이 끝났을 때 세션을 이어 붙여
        # 답을 받아낸다.
        "maxTurns": 0,
        # 상한이 없을 때 폭주를 막는 유일한 끈. 리뷰어 하나당 벽시계 시간.
        "timeoutSec": 3600,
        "maxDiffLines": 1500,
        "maxChangedFiles": 40,
        "skipDrafts": True,
        # head 가 바뀌면(=새 커밋 push) 다시 리뷰한다.
        "rereviewOnNewCommits": True,
        # 에이전트를 동시에 띄운다. 메모리가 빠듯하면 false 로 내린다.
        "parallel": True,
        # 이 확장자만 리뷰 대상으로 본다. 비우면 전부.
        "includeExts": [],
        # 리뷰어가 낸 지적을 코드 근거로 반증해 보는 단계. 워크트리가 아직
        # 살아 있는 동안 돈다 — 판정 단계는 코드를 못 보므로 거기서는 불가능하다.
        # 끄면 리뷰어의 말을 그대로 믿고 게시한다.
        "verify": {
            "enabled": True,
            # 좁은 확인 작업이라 작은 모델로 충분하다. 리뷰 비용에 비하면 미미하다.
            "model": "haiku",
            "timeoutSec": 300,
            "maxTurns": 15,
            # 동시에 띄울 검증자 수. 지적이 많아도 여기서 묶인다.
            "workers": 4,
        },
        # 경로에 이 조각이 들어가면 리뷰에서 제외 (lock 파일, 스냅샷 등)
        "excludeGlobs": ["package-lock.json", "yarn.lock", "pnpm-lock.yaml",
                         "poetry.lock", "go.sum", "__snapshots__/", "dist/",
                         "build/", ".min.js", ".min.css"],
    },
    "post": {
        # true 면 게시하지 않고 state/drafts/ 에만 남긴다.
        "dryRun": True,
        "maxFindingsPerPR": 15,
        # blocking | suggestion | nit — 이 등급 이상만 게시
        "minSeverity": "nit",
        "summaryComment": True,
        # true 면 판정 단계가 event 와 지적별 게시 형태를 스스로 정한다.
        # false 면 항상 COMMENT, 좌표가 맞는 지적은 inline, 나머지는 본문.
        "autonomous": True,
        # 판정이 낼 수 있는 이벤트. 여기서 빼면 그 판정은 COMMENT 로 내려간다.
        "allowedEvents": ["COMMENT", "APPROVE", "REQUEST_CHANGES"],
        "judgeModel": "opus",
        # 게시 직전에 문장만 다듬는다. 기계가 쓴 티(군더더기 서두, 빈 수식어, 과한
        # 헤징, 뻔한 맺음말)를 걷어내되 내용은 그대로 둔다. 다듬은 결과가 원문의
        # 파일:라인 참조나 코드 인용을 잃으면 그 항목은 원문으로 되돌린다.
        "polish": {
            "enabled": True,
            "model": "sonnet",
            "timeoutSec": 300,
        },
    },
    "limits": {
        # 한 틱에 리뷰할 PR 최대 개수. 틱 시간과 사용량에 직결된다.
        "maxPerTick": 2,
    },
    "notify": {"enabled": True, "sound": "Glass"},
    "gc": {
        # 리뷰가 성공하면 그 claude 세션 기록은 바로 버린다.
        "dropSessionOnSuccess": True,
        # 실패한 리뷰의 세션은 원인을 볼 단서라 며칠 남긴다.
        "sessionDays": 3,
        "draftDays": 14,
        "runsMaxLines": 2000,
        # 반증 기록은 지적 단위라 더 빨리 쌓이지만, 임계값을 정할 때 분포를
        # 봐야 하는 자료라 넉넉히 남긴다.
        "verifyMaxLines": 20000,
        "logMaxBytes": 5000000,
        # 날짜별 틱 로그 보존 기간. 하루치가 보통 수십 KB 라 넉넉히 둔다.
        "logDays": 30,
        "reviewedDays": 90,
    },
}

# altitude 는 설계 대안이라 우선순위가 아니지만, nit 과 같은 문턱으로 걸러진다.
SEVERITY_ORDER = {"blocking": 3, "suggestion": 2, "nit": 1, "altitude": 1}


def model_for(model_cfg, agent_name, fallback="opus"):
    """리뷰어 하나가 쓸 모델.

    문자열이면 리뷰어 전부가 그 모델을 쓴다. dict 면 에이전트 이름으로 찾고,
    없으면 "default" 로 떨어진다. 관점마다 필요한 추론 깊이가 달라서, 얕고 넓은
    리뷰까지 가장 비싼 모델로 돌릴 이유가 없다.
    """
    if isinstance(model_cfg, dict):
        return model_cfg.get(agent_name) or model_cfg.get("default") or fallback
    return model_cfg or fallback


def _merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load(path=None):
    path = path or os.path.join(ROOT, "config.json")
    cfg = DEFAULTS
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                cfg = _merge(DEFAULTS, json.load(f))
        except (ValueError, OSError) as e:
            raise SystemExit("config.json 을 읽지 못했습니다: %s" % e)
    return cfg


def load_raw(path=None):
    """config.json 에 **실제로 적힌** 값. DEFAULTS 를 섞지 않는다.

    설정을 고치는 명령은 이것을 읽어 고치고 다시 써야 한다. 병합된 값을 저장하면
    기본값이 사용자 파일에 눌러앉아, 나중에 기본값을 고쳐도 따라오지 않는다.
    """
    path = path or os.path.join(ROOT, "config.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError) as e:
        raise SystemExit("config.json 을 읽지 못했습니다: %s" % e)


def save_raw(raw, path=None):
    """원자적으로 쓴다 — 쓰다 죽어도 반쪽짜리 설정이 남지 않는다."""
    path = path or os.path.join(ROOT, "config.json")
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except OSError as e:
        raise SystemExit("config.json 을 쓰지 못했습니다: %s" % e)
    return path


def repo_entry(cfg, repo):
    """화이트리스트에 있고 켜져 있으면 설정을, 아니면 None 을 준다."""
    entry = (cfg.get("repos") or {}).get(repo)
    if not entry or entry.get("enabled") is False:
        return None
    entry = dict(entry)
    lp = entry.get("localPath")
    if lp:
        entry["localPath"] = os.path.expanduser(lp)
    return entry

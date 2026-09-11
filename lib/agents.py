"""~/.claude/agents/<name>.md 를 읽어 역할 정의를 꺼낸다.

에이전트 정의 파일이 여전히 single source of truth 다. 봇은 그 내용을 복사해
두지 않고 매 실행마다 읽어, 사용자가 에이전트를 고치면 봇 리뷰도 따라 바뀐다.

다만 그 정의는 lead 가 서브에이전트를 거느리는 대화형 세션을 전제로 쓰여 있다.
봇은 헤드리스 단발 프로세스라 lead 도, SendMessage 도, 쓰기 도구도 없다.
그래서 읽을 때 그쪽 전제에 묶인 섹션만 걷어낸다 — 원본은 건드리지 않는다.
"""
import os
import re

AGENTS_DIR = os.path.expanduser("~/.claude/agents")

# 봇 실행에서 수행 불가능하거나 프롬프트와 싸우는 섹션들. 지우는 이유는 각각:
#   When to invoke        봇은 이미 리뷰하기로 정해진 뒤에 뜬다
#   Workflow              "lead 에 scope 확인" 으로 시작한다 — 첫 턴을 질문으로
#                         끝내면 리뷰 결과가 통째로 버려진다
#   Output format         reports/*.md 를 쓰라 하고, 봇과 다른 severity 체계
#                         (Critical/High/…) 를 요구한다
#   Communication protocol  SendMessage 도, 받을 lead 도 없다
#   Handoff               다음 단계·재spawn 은 봇 바깥의 일이다
#   Definition of Done    "체크리스트 전 항목 점검" 이 작은 PR 에서도 턴을 끝까지 쓴다
#   HITL rule             "사용자 결정 대기" 를 할 사람이 헤드리스에는 없다
#
# 여기 없는 섹션은 모르는 것이라도 그대로 둔다. 새 에이전트를 물렸을 때
# 역할 정의가 통째로 비는 것보다 군더더기가 남는 편이 낫다.
DROP_SECTIONS = {
    "When to invoke",
    "Workflow",
    "Output format",
    "Communication protocol",
    "Handoff",
    "Definition of Done",
    "HITL rule",
}


class AgentError(Exception):
    pass


def load(name):
    """(frontmatter dict, 본문) 을 준다. 등록된 이름으로 찾는다."""
    path = os.path.join(AGENTS_DIR, name + ".md")
    if not os.path.exists(path):
        raise AgentError("에이전트 정의가 없습니다: %s" % path)
    return parse(path)


def parse(path):
    """아무 경로의 에이전트 .md 를 읽는다 — 등록 전 파일 검사에도 쓴다."""
    if not os.path.exists(path):
        raise AgentError("파일이 없습니다: %s" % path)
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError) as e:
        raise AgentError("읽지 못했습니다: %s" % e)

    meta, body = {}, text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end > 0:
            for line in text[3:end].splitlines():
                if ":" not in line or line.strip().startswith("#"):
                    continue
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip().strip('"').strip("'")
            body = text[end + 4:]
    return meta, body.strip()


def role(name):
    """봇 프롬프트에 끼울 역할 정의 — 헤드리스에서 못 할 지시는 걷어낸 본문."""
    _, body = load(name)
    trimmed = _strip_sections(body)
    # 통째로 비면 우리가 모르는 형식이라는 뜻이다. 빈 역할로 리뷰를 돌리느니
    # 군더더기째 넘긴다.
    return trimmed or body


def _strip_sections(body, drop=DROP_SECTIONS):
    """h1 섹션 단위로 잘라낸다. 코드 블록 안의 `# ` 는 헤더로 보지 않는다.

    HTML 주석도 걷어낸다 — 정의 파일의 주석은 그 파일을 읽는 사람에게 하는 말이지
    리뷰어에게 줄 지시가 아니다.
    """
    out, keep, fenced, in_comment = [], True, False, False
    for line in body.splitlines():
        if in_comment:
            in_comment = "-->" not in line
            continue
        stripped = line.strip()
        if line.lstrip().startswith("```"):
            fenced = not fenced
        elif not fenced:
            if stripped.startswith("<!--"):
                in_comment = "-->" not in stripped
                continue
            if line.startswith("# "):
                keep = line[2:].strip() not in drop
        if keep:
            out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()

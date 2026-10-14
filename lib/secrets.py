"""비밀 값을 keychain 에서 꺼내 온다.

여기를 거친 문자열은 인증 요청·자식 프로세스의 환경변수·전용 파이프에만 전달한다.
로그·예외 메시지·stdout 어디에도 싣지 않는다 — 그래서 이 모듈의 예외는 값은커녕 조회
명령의 stderr 도 담지 않는다. 실패 사유는 "없다/비었다" 수준까지만 알려 준다.

환경변수가 아니라 keychain 을 정본으로 두는 이유: launchd 는 로그인 셸을 거치지
않아 `.zshrc` 의 export 를 보지 못한다. 셸 프로필에만 토큰을 두면 손으로 돌릴
때는 되고 예약 틱에서만 조용히 401 이 나는, 가장 늦게 발견되는 형태의 고장이
된다.
"""
import os
import subprocess


class SecretError(Exception):
    """비밀 값을 꺼내지 못했다. 메시지에 값을 담지 않는다."""


def keychain(service, account=None, env_fallback=None):
    """keychain 의 generic password 를 돌려준다.

    `env_fallback` 이름의 환경변수가 있으면 keychain 이 비었을 때만 그쪽을 쓴다 —
    손으로 돌릴 때의 편의용이고, 예약 틱이 의지해서는 안 되는 경로다. 그래서
    doctor 가 이 경로로 떨어졌는지 따로 알려 준다.
    """
    cmd = ["security", "find-generic-password", "-w", "-s", service]
    if account:
        cmd += ["-a", account]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        # e 에는 명령줄만 담긴다 — 값은 인자로 준 적이 없으므로 안전하다.
        raise SecretError("keychain 조회에 실패했습니다: %s" % type(e).__name__)
    found = p.returncode == 0
    if found:
        token = (p.stdout or "").strip()
        if token:
            return token
    if env_fallback:
        token = (os.environ.get(env_fallback) or "").strip()
        if token:
            return token
    # 있는데 비었는지, 아예 없는지를 갈라 준다. 둘은 사람이 할 일이 다르고,
    # "없다" 고만 말하면 방금 등록한 사람이 등록을 또 하다 같은 자리에서 막힌다.
    # (빈 항목은 프롬프트에 값이 전달되지 않은 채 저장됐을 때 생긴다.)
    raise SecretError(
        "keychain 의 '%s' 가 비어 있습니다 — 값이 저장되지 않았습니다" % service
        if found else
        "keychain 에 '%s' 항목이 없습니다 — `review-bot doctor` 가 등록 방법을 알려 줍니다"
        % service)


def keychain_state(service, account=None):
    """"ok" | "empty" | "missing" — 값은 돌려주지 않는다. doctor 가 쓴다.

    항목이 있는지만 보면 부족하다. 값이 비어 있어도 `find-generic-password` 는
    성공하므로, 존재 여부만 물으면 "등록됨 ✓" 이라고 해 놓고 바로 다음 줄에서
    토큰을 못 꺼내는 모순이 나온다. 그래서 길이만 확인한다 — 값은 이 함수를
    벗어나지 않는다.
    """
    cmd = ["security", "find-generic-password", "-w", "-s", service]
    if account:
        cmd += ["-a", account]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return "missing"
    if p.returncode != 0:
        return "missing"
    return "ok" if (p.stdout or "").strip() else "empty"

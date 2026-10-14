"""Signed-app API bridge. Python never receives the Anthropic key."""
import json
import os
import subprocess


class APIError(Exception):
    pass


def call(payload, provider="claude"):
    if provider not in ("claude", "jev"):
        raise APIError("지원하지 않는 API입니다")
    label = "Jev" if provider == "jev" else "Claude"
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates = ['/Applications/ReviewBot.app/Contents/MacOS/ReviewBot',
                  os.path.join(root, 'app/ReviewBot.app/Contents/MacOS/ReviewBot')]
    executable = next((p for p in candidates if os.path.isfile(p)), None)
    if not executable:
        raise APIError('ReviewBot 앱을 빌드·설치하세요')
    timeout = 75 if provider == "jev" else max(5, min(180, int(payload.get("timeoutSec", 90)))) + 15
    try:
        result = subprocess.run([executable, '--jev-api' if provider == 'jev' else '--insight-api'], input=json.dumps(payload, ensure_ascii=False),
                                capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            raise APIError(label + ' API 보조 실행 실패')
        output = json.loads(result.stdout)
        if not isinstance(output, dict): raise ValueError()
    except (OSError, subprocess.SubprocessError, ValueError):
        raise APIError(label + ' API 연결 또는 응답 해석 실패') from None
    error = output.get('error')
    if error == 'keychain_locked': raise APIError('키체인 접근이 필요합니다. 앱 설정의 키체인 접근 확인을 눌러 주세요. 자동 실행에서는 암호 창을 열지 않습니다.')
    if error == 'missing_key': raise APIError('설정에서 ' + label + ' API 키를 등록하세요')
    if error == 'http':
        status = output.get('status')
        if not isinstance(status, int): raise APIError('Claude API 오류')
        hint = ' · Workspace ID와 키의 접근 권한을 확인하세요' if provider == 'claude' and status in (400, 401, 403) else ''
        raise APIError(label + ' API HTTP %d%s' % (status, hint))
    if error: raise APIError(label + ' API 연결 또는 요청 처리 실패')
    return output


def key_state():
    try:
        state = call({'action': 'status'}).get('keyState')
        return state if state in ('ok', 'missing', 'locked') else 'unavailable'
    except APIError:
        return 'unavailable'

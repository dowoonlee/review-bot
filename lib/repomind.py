"""사내 RepoMind MCP 서버 연결.

리뷰어는 워크트리의 코드는 다 읽을 수 있지만 **이 코드가 왜 이렇게 됐는지**는
모른다. 봇이 직접 모아 주는 커밋 이력·과거 코멘트는 파일 10개·커밋 5건이 상한인
맛보기라, 큰 PR 에서는 사실상 없는 것과 같다. RepoMind 는 그 자리를 메운다.

두 가지를 지킨다.

- **토큰은 디스크에 남기지 않는다.** 여기서 쓰는 MCP 설정 파일에는
  `${REPOMIND_TOKEN}` 참조만 적히고, 값은 자식 프로세스의 환경변수로만 흘러간다.
- **못 닿으면 조용히 넘어가지 않는다.** 서버가 죽었거나 토큰이 만료되면 리뷰어는
  도구만 못 부를 뿐 리뷰 자체는 그럴듯하게 끝난다. 이력 없는 리뷰가 몇 주 나가도
  모르는 것이 가장 나쁜 결말이라, 리뷰를 시작하기 전에 한 번 찔러 본다.
"""
import json
import os
import urllib.error
import urllib.request

from lib import secrets

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.join(ROOT, "state")
CONFIG_PATH = os.path.join(STATE_DIR, "mcp-repomind.json")

# MCP 설정에 적히는 참조 이름. 값이 아니라 이 문자열이 파일에 들어간다.
ENV_VAR = "REPOMIND_TOKEN"
SERVER = "repomind"


class RepomindError(Exception):
    """정찰 경로가 끊겼다."""


class Repomind:
    """리뷰어에게 열어 줄 RepoMind 접속 한 벌.

    토큰은 이 객체 안에만 있고 `env()` 로만 새어 나간다. `__repr__` 을 덮어 두는
    것은 예외 추적이나 디버그 출력에 객체가 통째로 찍혀도 값이 드러나지 않게
    하기 위해서다.
    """

    def __init__(self, url, token, tools, timeout=20):
        self.url = url
        self._token = token
        self.tools = ["mcp__%s__%s" % (SERVER, t) for t in tools]
        self.timeout = timeout
        self.config_path = CONFIG_PATH

    def __repr__(self):
        return "<Repomind url=%s tools=%d>" % (self.url, len(self.tools))

    def _scrub(self, msg):
        """밖으로 나가는 문장에서 토큰을 지운다.

        소켓·HTTP 오류가 토큰을 담을 일은 보통 없지만, 그 "보통" 에 기대지
        않는다. 이 메시지는 로그와 macOS 알림으로 나가므로 한 번만 새도
        영구히 남는다. 예외 본문을 문장에 섞는 곳은 전부 이 문을 지난다.
        """
        text = str(msg)
        return text.replace(self._token, "***") if self._token else text

    def env(self):
        """자식 프로세스에 얹을 환경변수. 이 반환값을 로그에 찍지 말 것."""
        return {ENV_VAR: self._token}

    def write_config(self):
        """MCP 설정 파일을 쓴다 — 토큰 자리에는 `${...}` 참조만 들어간다."""
        os.makedirs(STATE_DIR, exist_ok=True)
        doc = {"mcpServers": {SERVER: {
            "type": "http",
            "url": self.url,
            "headers": {"Authorization": "Bearer ${%s}" % ENV_VAR},
        }}}
        tmp = self.config_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.config_path)
        return self.config_path

    def preflight(self):
        """서버가 살아 있고 토큰이 통하는지 확인한다. 실패하면 RepomindError.

        MCP 의 `initialize` 를 한 번 보낸다. 401 과 연결 실패를 갈라서 알려 주는
        것이 중요하다 — 전자는 토큰을 갱신하라는 뜻이고 후자는 사내망 밖이라는
        뜻이라, 사람이 할 일이 다르다.
        """
        body = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "review-bot-preflight", "version": "1"}},
        }).encode("utf-8")
        req = urllib.request.Request(self.url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json, text/event-stream")
        req.add_header("Authorization", "Bearer %s" % self._token)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                if resp.status >= 400:
                    raise RepomindError("서버가 %s 를 돌려줬습니다" % resp.status)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise RepomindError(
                    "401 — 토큰이 거부됐습니다. keychain 의 값이 만료됐는지 보세요")
            raise RepomindError("서버가 %s 를 돌려줬습니다" % e.code)
        except urllib.error.URLError as e:
            raise RepomindError("%s 에 닿지 못했습니다 (%s)"
                                % (self.url, self._scrub(e.reason)))
        except OSError as e:
            raise RepomindError("%s 에 닿지 못했습니다 (%s)"
                                % (self.url, self._scrub(e)))
        return True


def connect(cfg):
    """설정과 keychain 으로 Repomind 를 만든다. 꺼져 있으면 None.

    토큰을 꺼내는 것까지만 하고 네트워크는 건드리지 않는다 — 찔러 보는 것은
    호출자가 `preflight()` 로 명시적으로 한다.
    """
    rc = (cfg or {}).get("repomind") or {}
    if not rc.get("enabled"):
        return None
    url = (rc.get("url") or "").strip()
    if not url:
        raise RepomindError("repomind.url 이 비어 있습니다")
    tools = rc.get("tools") or []
    if not tools:
        raise RepomindError("repomind.tools 가 비어 있습니다")
    try:
        token = secrets.keychain(rc.get("keychainService") or "review-bot-repomind",
                                 rc.get("keychainAccount") or None,
                                 env_fallback=ENV_VAR)
    except secrets.SecretError as e:
        raise RepomindError(str(e))
    rm = Repomind(url, token, tools, rc.get("timeoutSec") or 20)
    rm.write_config()
    return rm

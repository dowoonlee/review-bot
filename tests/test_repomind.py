"""RepoMind 정찰 배선 — 토큰이 새지 않는지, 못 닿을 때 조용하지 않은지.

이 두 가지가 이 기능의 전부다. 정찰이 붙어서 리뷰가 좋아지는 것은 검증할 수
없지만, 토큰이 디스크에 남는 것과 서버가 죽었는데 리뷰가 그냥 나가는 것은
코드에서 막을 수 있다.
"""
import json
import unittest
from unittest import mock

from lib import repomind, reviewer, secrets

TOKEN = "test-token-do-not-log-me"
CFG = {"repomind": {"enabled": True, "url": "http://rm.test/mcp",
                    "keychainService": "svc",
                    "tools": ["impact", "search"], "timeoutSec": 5}}


def _connected(tmpdir):
    rm = repomind.Repomind("http://rm.test/mcp", TOKEN, ["impact", "search"])
    rm.config_path = str(tmpdir / "mcp.json")
    return rm


class TokenNeverOnDiskTests(unittest.TestCase):
    """토큰은 환경변수로만 흐른다. 설정 파일에는 참조만 적힌다."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_config_file_holds_a_reference_not_the_value(self):
        rm = _connected(self.dir)
        rm.write_config()
        text = open(rm.config_path, encoding="utf-8").read()
        self.assertNotIn(TOKEN, text)
        self.assertIn("${REPOMIND_TOKEN}", text)
        doc = json.loads(text)
        self.assertEqual(doc["mcpServers"]["repomind"]["url"], "http://rm.test/mcp")

    def test_repr_does_not_leak(self):
        rm = _connected(self.dir)
        self.assertNotIn(TOKEN, repr(rm))

    def test_preflight_error_does_not_leak(self):
        """실패 메시지는 로그로도 알림으로도 나간다 — 거기 값이 섞이면 안 된다."""
        rm = _connected(self.dir)
        with mock.patch("urllib.request.urlopen", side_effect=OSError(TOKEN)):
            with self.assertRaises(repomind.RepomindError) as cm:
                rm.preflight()
        # OSError 본문이 그대로 실리면 이 단언이 깨진다 — 그때는 메시지를 좁혀야 한다.
        self.assertNotIn(TOKEN, str(cm.exception))

    def test_secret_error_carries_no_value(self):
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=1, stdout="", stderr=TOKEN)
            with self.assertRaises(secrets.SecretError) as cm:
                secrets.keychain("svc")
        self.assertNotIn(TOKEN, str(cm.exception))


class KeychainStateTests(unittest.TestCase):
    """빈 항목과 없는 항목은 다르다.

    `security` 는 값이 비어 있어도 조회에 성공한다. 존재 여부만 물으면
    "등록됨 ✓" 이라 해 놓고 다음 줄에서 토큰을 못 꺼내는 모순이 나오고,
    방금 등록한 사람이 같은 자리에서 또 막힌다. 실제로 그렇게 막혔다.
    """

    def _state(self, returncode, stdout):
        with mock.patch("subprocess.run",
                        return_value=mock.Mock(returncode=returncode, stdout=stdout,
                                               stderr="")):
            return secrets.keychain_state("svc")

    def test_present_and_filled(self):
        self.assertEqual(self._state(0, TOKEN), "ok")

    def test_present_but_empty(self):
        self.assertEqual(self._state(0, "\n"), "empty")

    def test_absent(self):
        self.assertEqual(self._state(44, ""), "missing")

    def test_empty_and_missing_say_different_things(self):
        def msg(returncode, stdout):
            with mock.patch("subprocess.run",
                            return_value=mock.Mock(returncode=returncode,
                                                   stdout=stdout, stderr="")):
                with self.assertRaises(secrets.SecretError) as cm:
                    secrets.keychain("svc")
            return str(cm.exception)
        self.assertIn("비어 있습니다", msg(0, ""))
        self.assertIn("항목이 없습니다", msg(44, ""))


class CommandLineTests(unittest.TestCase):
    """토큰은 명령줄에 오르지 않는다 — 명령줄은 ps 로 아무나 읽는다."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.seen = {}

        def fake_invoke(cmd, prompt, cwd, timeout, env_extra=None):
            self.seen["cmd"] = cmd
            self.seen["env"] = env_extra
            self.seen["prompt"] = prompt
            return json.dumps({"result": '{"findings": []}', "total_cost_usd": 0}), "", 0

        patcher = mock.patch.object(reviewer, "_invoke", fake_invoke)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_task_identity_is_preserved_in_the_session_prompt(self):
        reviewer._run("review contents", str(self.dir), "haiku", 0, 10,
                      "batch-002-silent-failure-hunter")
        self.assertEqual(self.seen["prompt"],
                         "<!-- review-bot-task: batch-002-silent-failure-hunter -->\nreview contents")

    def test_mcp_goes_to_config_flag_and_token_to_env(self):
        rm = _connected(self.dir)
        rm.write_config()
        reviewer._run("p", str(self.dir), "haiku", 0, 10, "t", mcp=rm)
        cmd = self.seen["cmd"]
        self.assertNotIn(TOKEN, " ".join(cmd))
        self.assertIn("--mcp-config", cmd)
        self.assertEqual(self.seen["env"], {"REPOMIND_TOKEN": TOKEN})

    def test_hardening_flags_survive(self):
        """MCP 를 하나 붙여도 나머지 봉인은 그대로여야 한다."""
        rm = _connected(self.dir)
        rm.write_config()
        reviewer._run("p", str(self.dir), "haiku", 0, 10, "t", mcp=rm)
        cmd = self.seen["cmd"]
        self.assertIn("--restricted", cmd)
        self.assertIn("--strict-mcp-config", cmd)
        self.assertEqual(cmd[cmd.index("--tools") + 1], "Read,Grep,Glob")
        allowed = cmd[cmd.index("--allowedTools") + 1]
        self.assertIn("mcp__repomind__impact", allowed)
        self.assertIn("Read,Grep,Glob", allowed)

    def test_no_mcp_flag_when_disconnected(self):
        reviewer._run("p", str(self.dir), "haiku", 0, 10, "t", mcp=None)
        self.assertNotIn("--mcp-config", self.seen["cmd"])
        self.assertIsNone(self.seen["env"])

    def test_toolless_steps_never_get_mcp(self):
        """판정·다듬기는 도구가 없다. 거기에 서버를 달면 안 된다."""
        rm = _connected(self.dir)
        reviewer._run("p", str(self.dir), "haiku", 1, 10, "t", tools="", mcp=rm)
        self.assertNotIn("--mcp-config", self.seen["cmd"])
        self.assertIsNone(self.seen["env"])


class PromptTests(unittest.TestCase):
    def test_recon_block_empty_without_server(self):
        self.assertEqual(reviewer._recon_block(None), "")

    def test_recon_block_names_only_granted_tools(self):
        rm = repomind.Repomind("http://rm.test/mcp", TOKEN, ["impact", "search"])
        block = reviewer._recon_block(rm)
        self.assertIn("impact", block)
        self.assertIn("search", block)
        self.assertNotIn("coupling", block)
        self.assertNotIn(TOKEN, block)


class ConnectTests(unittest.TestCase):
    def test_disabled_gives_nothing(self):
        self.assertIsNone(repomind.connect({"repomind": {"enabled": False}}))

    def test_missing_url_is_an_error_not_a_shrug(self):
        with self.assertRaises(repomind.RepomindError):
            repomind.connect({"repomind": {"enabled": True, "url": "",
                                           "tools": ["impact"]}})

    def test_missing_token_is_an_error_not_a_shrug(self):
        with mock.patch.object(secrets, "keychain",
                               side_effect=secrets.SecretError("없습니다")):
            with self.assertRaises(repomind.RepomindError):
                repomind.connect(CFG)


if __name__ == "__main__":
    unittest.main()


class TickBlockingTests(unittest.TestCase):
    """정찰이 끊겼을 때 리뷰가 조용히 나가지 않는지.

    이 봇의 기존 실패 방식이 "건너뛰고 아무 말 없음" 이었다. 같은 모양이
    정찰에서 되풀이되면 이력 없는 리뷰가 몇 주 동안 게시된다.
    """

    def _tick(self):
        import runpy
        from pathlib import Path
        return runpy.run_path(str(Path(__file__).resolve().parents[1] / "review-bot"))

    def _run(self, required):
        import tempfile
        from lib import github, notify as notify_mod, replies, store as store_mod
        # 틱이 PR 단위 lock 파일을 쓰므로 실제 state 디렉터리를 건드리지 않게 한다.
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        state_dir = mock.patch.object(store_mod, "STATE_DIR", tmp.name); state_dir.start()
        self.addCleanup(state_dir.stop)
        bot = self._tick()
        tick = bot["_tick"]
        seen = []

        def process(item, cfg, state, mcp=None):
            seen.append(mcp)
            return {"skipped": "x"}

        cfg = {"repos": {"org/repo": {"account": "me"}}, "limits": {}, "notify": {},
               "repomind": {"enabled": True, "url": "http://rm.test/mcp",
                            "tools": ["impact"], "required": required}}
        with mock.patch.dict(tick.__globals__,
                             {"_gc": lambda _: None, "_log": lambda _: None,
                              "process_pr": process}), \
                mock.patch.object(store_mod, "load", return_value={}), \
                mock.patch.object(store_mod, "log_run") as logged, \
                mock.patch.object(github, "whoami", return_value="me"), \
                mock.patch.object(github, "review_requests", return_value=[
                    {"repo": "org/repo", "number": 1, "title": "t", "author": "a"}]), \
                mock.patch.object(github, "sent_review_requests", return_value=([], [])), \
                mock.patch.object(github, "assigned_issues", return_value=([], [])), \
                mock.patch.object(replies, "check"), \
                mock.patch.object(notify_mod, "send") as notified, \
                mock.patch.object(repomind, "connect",
                                  side_effect=repomind.RepomindError("401 — 토큰이 거부됐습니다")):
            rc = tick(cfg)
        return rc, seen, notified, logged

    def test_required_stops_the_tick_and_says_so(self):
        rc, seen, notified, logged = self._run(required=True)
        self.assertEqual(rc, 1)
        self.assertEqual(seen, [])          # 한 건도 리뷰하지 않았다
        notified.assert_called_once()
        self.assertIn("정찰 불가", notified.call_args.args[0])
        self.assertTrue(logged.call_args.kwargs.get("repomindDown")
                        or logged.call_args.args[0].get("repomindDown"))

    def test_optional_continues_but_still_notifies(self):
        rc, seen, notified, _ = self._run(required=False)
        self.assertEqual(rc, 0)
        self.assertEqual(seen, [None])      # 정찰 없이 리뷰했다
        notified.assert_called_once()       # 그래도 알린다

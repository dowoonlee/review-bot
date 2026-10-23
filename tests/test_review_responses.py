import json
import runpy
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lib import github, notify, replies, store


def pr(response_id="R1", state="APPROVED", author="reviewer"):
    return {"repo": "org/repo", "number": 1, "title": "Fix", "author": "me",
            "responses": [{"id": response_id, "author": author, "state": state}]}


class ResponseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = patch.object(store, "STATE_DIR", self.tmp.name)
        self.root.start()
        self.addCleanup(self.root.stop)
        self.send = patch.object(notify, "send", return_value=True).start()
        self.addCleanup(patch.stopall)
        self.logs = []

    def check(self, prs, account="me", enabled=True):
        return replies.check(prs, account, {"enabled": enabled, "sound": "Glass"}, self.logs.append)

    def test_first_observation_is_silent_and_same_review_is_not_repeated(self):
        self.check([pr()])
        self.check([pr()])
        self.send.assert_not_called()
        self.assertEqual(self.check([pr("R2")]), 1)
        self.check([pr("R2")])
        self.send.assert_called_once()
        self.assertIn("reviewer 승인", self.send.call_args.args[1])

    def test_empty_baseline_then_new_comment_or_change_request(self):
        self.check([])
        self.check([pr(state="COMMENTED")])
        self.check([pr("R2", "CHANGES_REQUESTED")])
        self.assertEqual(self.send.call_count, 2)
        self.assertIn("변경 요청", self.send.call_args.args[1])

    def test_failed_delivery_is_retried(self):
        self.check([])
        self.send.return_value = False
        self.assertEqual(self.check([pr()]), 0)
        self.send.return_value = True
        self.assertEqual(self.check([pr()]), 1)
        self.assertEqual(self.check([pr()]), 0)
        self.assertEqual(self.send.call_count, 2)

    def test_disabled_notifications_are_consumed_and_own_reviews_ignored(self):
        self.check([])
        self.check([pr()], enabled=False)
        self.check([pr()])
        self.check([pr("SELF", author="ME")])
        self.send.assert_not_called()

    def test_accounts_have_independent_baselines_and_closed_prs_do_not_reset_seen(self):
        self.check([pr()], account="a")
        self.check([pr("R2")], account="b")
        self.check([], account="a")
        self.check([pr()], account="a")
        self.send.assert_not_called()
        self.check([pr("R2")], account="a")
        self.send.assert_called_once()

    def test_multiple_reviewers_are_grouped_per_pr(self):
        self.check([])
        item = pr()
        item["responses"].append({"id": "R2", "author": "other", "state": "CHANGES_REQUESTED"})
        self.assertEqual(self.check([item]), 2)
        self.send.assert_called_once()
        self.assertIn("other 변경 요청", self.send.call_args.args[1])

    def test_corrupt_checkpoint_is_not_overwritten(self):
        path = Path(self.tmp.name) / "review-responses.json"
        path.write_text('{"me": "invalid"}')
        with self.assertRaises(ValueError):
            self.check([pr()])
        self.assertEqual(json.loads(path.read_text()), {"me": "invalid"})
        self.send.assert_not_called()


class TickResponseTests(unittest.TestCase):
    def setUp(self):
        # 틱이 PR 단위 lock 파일을 쓰므로 실제 state 디렉터리를 건드리지 않게 한다.
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        p = patch.object(store, "STATE_DIR", self.tmp.name); p.start(); self.addCleanup(p.stop)

    def test_queries_run_together_and_received_failure_does_not_block_replies(self):
        bot = runpy.run_path(str(Path(__file__).resolve().parents[1] / "review-bot"))
        tick = bot["_tick"]
        barrier = threading.Barrier(2, timeout=3)

        def received(**kwargs):
            barrier.wait()
            raise github.GhError("offline")

        def sent(**kwargs):
            barrier.wait()
            return [pr()], []

        cfg = {"repos": {"org/repo": {"account": "me"}}, "limits": {}, "notify": {}}
        with patch.dict(tick.__globals__, {"_gc": lambda _: None, "_log": lambda _: None}), \
             patch.object(store, "load", return_value={}), \
             patch.object(github, "whoami", return_value="me"), \
             patch.object(github, "review_requests", side_effect=received), \
             patch.object(github, "sent_review_requests", side_effect=sent), \
             patch.object(github, "assigned_issues", return_value=([], [])), \
             patch.object(replies, "check") as check:
            self.assertEqual(tick(cfg), 0)
        check.assert_called_once()

    def test_response_failure_does_not_block_incoming_review(self):
        bot = runpy.run_path(str(Path(__file__).resolve().parents[1] / "review-bot"))
        tick = bot["_tick"]
        processed = []
        def process(item, cfg, state, mcp=None):
            processed.append(item["number"])
            return {"skipped": "already reviewed"}
        cfg = {"repos": {"org/repo": {"account": "me"}}, "limits": {}, "notify": {}}
        with patch.dict(tick.__globals__, {"_gc": lambda _: None, "_log": lambda _: None, "process_pr": process}), \
             patch.object(store, "load", return_value={}), \
             patch.object(github, "whoami", return_value="me"), \
             patch.object(github, "review_requests", return_value=[pr()]), \
             patch.object(github, "sent_review_requests", side_effect=github.GhError("offline")), \
             patch.object(github, "assigned_issues", return_value=([], [])):
            self.assertEqual(tick(cfg), 0)
        self.assertEqual(processed, [1])


class NotifyTests(unittest.TestCase):
    def test_nonzero_exit_is_failure_and_truncation_keeps_quotes_escaped(self):
        with patch.object(notify.subprocess, "run", return_value=SimpleNamespace(returncode=1)) as run:
            self.assertFalse(notify.send("title", 'x' * 239 + '"tail'))
        self.assertIn('x\\"" with title', run.call_args.args[0][2])


if __name__ == "__main__":
    unittest.main()

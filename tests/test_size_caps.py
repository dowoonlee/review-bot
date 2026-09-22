"""크기 상한 — 두 축을 함께 알리고, 건너뛴 것을 조용히 넘기지 않는다.

상한 자체는 있을 이유가 있지만, 걸렸을 때 로그 한 줄만 남기고 "새로 리뷰할 PR
없음" 으로 끝내면 사용자는 봇이 고장 났는지 할 일이 없는지 알 수 없다. 실제로
#6156 이 그 상태로 매 틱 버려졌다.
"""
import runpy
import unittest
from pathlib import Path
from unittest import mock

from lib import reviewer, store


def _bot():
    return runpy.run_path(str(Path(__file__).resolve().parents[1] / "review-bot"))


class DiffBudgetTests(unittest.TestCase):
    """줄 상한과 글자 상한이 어긋나지 않는다.

    둘을 따로 두면 줄 상한을 올린 순간 리뷰어가 diff 없이 파일을 보게 되고,
    그것이 옮겨진 코드를 새 코드로 착각하는 조건이다.
    """

    def test_budget_follows_the_line_cap(self):
        self.assertGreater(reviewer.diff_char_cap(3000), reviewer.diff_char_cap(1500))

    def test_budget_covers_the_whole_diff_at_the_cap(self):
        for lines in (1000, 1500, 3000):
            # 실측 77.4자/줄. 상한만큼 꽉 찬 diff 가 잘리지 않아야 한다.
            self.assertGreaterEqual(reviewer.diff_char_cap(lines), lines * 78,
                                    "%d줄짜리 diff 가 잘린다" % lines)

    def test_ceiling_protects_the_context_window(self):
        self.assertEqual(reviewer.diff_char_cap(10**6), reviewer.DIFF_CHAR_CEILING)

    def test_unlimited_uses_full_context_budget(self):
        self.assertEqual(reviewer.diff_char_cap(0), reviewer.DIFF_CHAR_CEILING)

    def test_missing_cap_falls_back(self):
        self.assertEqual(reviewer.diff_char_cap(None), reviewer.DIFF_CHAR_CAP)

    def test_oversized_diff_names_what_it_dropped(self):
        files = [{"filename": "a.py", "patch": "+x" * 50000},
                 {"filename": "b.py", "patch": "+y" * 10}]
        block = reviewer._diff_block(files, cap=1000)
        self.assertIn("워크트리에서 직접 읽으세요", block)
        self.assertIn("a.py", block)


class SizeGateTests(unittest.TestCase):
    def test_limits_can_be_disabled_independently(self):
        bot = _bot()
        gh = bot["gh"]
        files = [{"filename": "f%d.py" % i, "additions": 87} for i in range(136)]
        for file_cap, line_cap, reason in ((0, 0, None), (80, 0, "파일"),
                                           (0, 3000, "줄")):
            with self.subTest(file_cap=file_cap, line_cap=line_cap):
                cfg = {"repos": {"o/r": {"enabled": True}}, "post": {},
                       "review": {"maxChangedFiles": file_cap, "maxDiffLines": line_cap}}
                with mock.patch.object(gh, "active_account", return_value="me"), \
                        mock.patch.object(gh, "env_for", return_value={}), \
                        mock.patch.object(gh, "pr_meta", return_value={"state": "OPEN", "headRefOid": "s"}), \
                        mock.patch.object(gh, "pr_files", return_value=files), \
                        mock.patch.object(gh, "last_review_request", return_value=None), \
                        mock.patch.object(gh, "ci_state", side_effect=RuntimeError("passed size gate")):
                    if reason is None:
                        with self.assertRaisesRegex(RuntimeError, "passed size gate"):
                            bot["process_pr"]({"repo": "o/r", "number": 6156}, cfg, {})
                    else:
                        result = bot["process_pr"]({"repo": "o/r", "number": 6156}, cfg, {})
                        self.assertEqual(result["skipKind"], "size")
                        self.assertIn(reason, result["skipped"])


class SkipAnnouncementTests(unittest.TestCase):
    """커밋 하나당 한 번만 알린다 — 매 틱 울리면 사람이 알림을 끈다."""

    def test_same_commit_and_reason_is_announced_once(self):
        state = {}
        self.assertFalse(store.skip_announced(state, "o/r", 1, "sha1", "큼"))
        store.mark_skip_announced(state, "o/r", 1, "sha1", "큼")
        self.assertTrue(store.skip_announced(state, "o/r", 1, "sha1", "큼"))

    def test_new_commit_is_announced_again(self):
        state = {}
        store.mark_skip_announced(state, "o/r", 1, "sha1", "큼")
        self.assertFalse(store.skip_announced(state, "o/r", 1, "sha2", "큼"))

    def test_changed_reason_is_announced_again(self):
        state = {}
        store.mark_skip_announced(state, "o/r", 1, "sha1", "파일 초과")
        self.assertFalse(store.skip_announced(state, "o/r", 1, "sha1", "줄 초과"))

    def test_skip_record_does_not_count_as_reviewed(self):
        """건너뜀 기록이 `already_done` 에 걸리면 상한을 올려도 영영 안 본다."""
        state = {}
        store.mark_skip_announced(state, "o/r", 1, "sha1", "큼")
        self.assertFalse(store.already_done(state, "o/r", 1, "sha1", True))

    def test_only_size_skips_are_announced(self):
        bot = _bot()
        announce = bot["_announce_skip"]
        cfg = {"notify": {"enabled": False}}
        for kind in (None, "draft", "already"):
            r = {"skipKind": kind, "repo": "o/r", "number": 1, "sha": "s",
                 "skipped": "이미 리뷰함"}
            self.assertFalse(announce(r, cfg, {}))

    def test_size_skip_notifies_and_records_once(self):
        bot = _bot()
        announce = bot["_announce_skip"]
        cfg = {"notify": {"enabled": True, "sound": "Glass"}}
        state = {}
        r = {"skipKind": "size", "repo": "o/r", "number": 6156, "sha": "s1",
             "skipped": "파일 120개(상한 80) · 11123줄(상한 3000) — 상한 초과",
             "files": 120, "churn": 11123, "title": "t"}
        with mock.patch.dict(bot["_announce_skip"].__globals__,
                             {"_log": lambda _: None}), \
                mock.patch.object(store, "log_run") as logged, \
                mock.patch.object(store, "save"), \
                mock.patch.object(bot["notify"], "send") as sent:
            self.assertTrue(announce(r, cfg, state))
            self.assertFalse(announce(r, cfg, state))   # 두 번째는 조용히
        logged.assert_called_once()
        sent.assert_called_once()
        self.assertIn("6156", sent.call_args.args[1])
        self.assertEqual(logged.call_args.args[0]["files"], 120)


if __name__ == "__main__":
    unittest.main()

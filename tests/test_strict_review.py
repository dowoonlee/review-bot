"""범위 판정·승인 문턱·CI 안전장치 — 6053 리뷰에서 드러난 구멍을 막는 규칙들.

base 에 그대로 있던 코드의 문제가 "이 PR 의 지적" 으로 게시됐고, P2 를 두 건
남긴 채 ✅ Approve 가 나갔다. 여기서는 그 두 경로가 코드에서 막히는지 본다.
"""
import runpy
import unittest
import tempfile
from unittest import mock
from pathlib import Path

from lib import github, reviewer

PATCH = "@@ -10,4 +10,5 @@\n ctx\n-old\n+new1\n+new2\n ctx2\n"
FILES = {"a.py": {"postable": github.postable_lines(PATCH),
                  "added": github.added_lines(PATCH), "patch": PATCH}}


def _bot():
    return runpy.run_path(str(Path(__file__).resolve().parents[1] / "review-bot"))


def finding(path="a.py", line=11, severity="suggestion", title="t"):
    return {"path": path, "line": line, "severity": severity, "title": title, "body": "b"}


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.scope = _bot()["_scope_findings"]

    def test_added_line_is_in_scope(self):
        kept, dropped = self.scope([finding(line=11)], FILES)
        self.assertEqual([f["_scope"] for f in kept], ["added"])
        self.assertEqual(dropped, [])

    def test_hidden_paths_survive_scope_and_inline_validation(self):
        bot = _bot()
        for name in (".github/workflows/ci.yml", ".pre-commit-config.yaml"):
            with self.subTest(name=name):
                files = {name: FILES["a.py"]}
                for supplied in (name, "./" + name):
                    kept, dropped = self.scope([finding(path=supplied)], files)
                    self.assertEqual(dropped, [])
                    inline, demoted = bot["_validate"](kept, files, {})
                    self.assertEqual(demoted, [])
                    self.assertEqual(inline[0]["path"], name)

    def test_context_line_is_kept_but_marked(self):
        kept, _ = self.scope([finding(line=10)], FILES)
        self.assertEqual(kept[0]["_scope"], "context")

    def test_outside_diff_is_dropped_before_verify(self):
        kept, dropped = self.scope([finding(line=277)], FILES)
        self.assertEqual(kept, [])
        self.assertEqual(dropped[0]["_scope"], "outside")
        self.assertEqual(dropped[0]["_why"], "diff 밖의 라인")

    def test_unknown_path_is_dropped(self):
        _, dropped = self.scope([finding(path="zzz.py")], FILES)
        self.assertEqual(dropped[0]["_why"], "변경 파일 목록에 없는 경로")

    def test_line_null_survives_as_unknown(self):
        kept, _ = self.scope([finding(line=None)], FILES)
        self.assertEqual(kept[0]["_scope"], "unknown")

    def test_lenient_mode_keeps_outside_for_demotion(self):
        kept, dropped = self.scope([finding(line=277)], FILES, strict=False)
        self.assertEqual(dropped, [])
        self.assertEqual(kept[0]["_scope"], "outside")


class HiddenPathVerificationTests(unittest.TestCase):
    def test_verifier_receives_hidden_file_diff(self):
        with tempfile.TemporaryDirectory() as cwd, mock.patch.object(reviewer, "_run", return_value={
                "verdict": "TRUE_POSITIVE", "impact": "concrete", "reason": "confirmed"}) as run:
            reviewer.verify([finding(path="./.github/workflows/ci.yml")],
                            {"_repo": "o/r", "_number": 1}, cwd,
                            patches={".github/workflows/ci.yml": PATCH})
            self.assertIn("+new1", run.call_args.args[0])


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.guard = _bot()["_guard_event"]
        self.pcfg = {"allowedEvents": ["COMMENT", "APPROVE", "REQUEST_CHANGES"]}

    def test_approve_with_remaining_p2_is_downgraded(self):
        event, why = self.guard("APPROVE", [finding(severity="suggestion")], {}, self.pcfg)
        self.assertEqual(event, "COMMENT")
        self.assertIn("확인", why)

    def test_approve_with_only_nits_passes(self):
        event, why = self.guard("APPROVE", [finding(severity="nit"),
                                            finding(severity="altitude")], {}, self.pcfg)
        self.assertEqual((event, why), ("APPROVE", ""))

    def test_legacy_threshold_cannot_approve_an_unclassified_finding(self):
        pcfg = dict(self.pcfg, approveMaxSeverity="suggestion")
        event, _ = self.guard("APPROVE", [finding(severity="suggestion")], {}, pcfg)
        self.assertEqual(event, "COMMENT")

    def test_ci_failure_blocks_approve(self):
        event, why = self.guard("APPROVE", [], {}, self.pcfg, ci="failure")
        self.assertEqual(event, "COMMENT")
        self.assertIn("CI", why)

    def test_ci_none_does_not_block(self):
        event, _ = self.guard("APPROVE", [], {}, self.pcfg, ci="none")
        self.assertEqual(event, "APPROVE")

    def test_ci_guard_can_be_disabled(self):
        pcfg = dict(self.pcfg, approveNeedsGreenCi=False)
        event, _ = self.guard("APPROVE", [], {}, pcfg, ci="failure")
        self.assertEqual(event, "APPROVE")


class CiStateTests(unittest.TestCase):
    def test_no_checks_is_none_not_success(self):
        self.assertEqual(github.ci_state({"statusCheckRollup": []}), "none")
        self.assertEqual(github.ci_state({}), "none")

    def test_any_failure_wins(self):
        meta = {"statusCheckRollup": [
            {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "FAILURE"}]}
        self.assertEqual(github.ci_state(meta), "failure")

    def test_pending_beats_success(self):
        meta = {"statusCheckRollup": [
            {"__typename": "CheckRun", "status": "IN_PROGRESS", "conclusion": None},
            {"__typename": "StatusContext", "state": "SUCCESS"}]}
        self.assertEqual(github.ci_state(meta), "pending")

    def test_all_green(self):
        meta = {"statusCheckRollup": [
            {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"__typename": "StatusContext", "state": "SUCCESS"}]}
        self.assertEqual(github.ci_state(meta), "success")


class DiffBlockTests(unittest.TestCase):
    def test_patch_is_fenced_per_file(self):
        block = reviewer._diff_block([{"filename": "a.py", "patch": PATCH}])
        self.assertIn("### `a.py`", block)
        self.assertIn("```diff\n" + PATCH, block)

    def test_missing_patch_is_named_not_silently_skipped(self):
        block = reviewer._diff_block([{"filename": "big.bin", "patch": None}])
        self.assertIn("big.bin", block)
        self.assertIn("직접 읽으세요", block)

    def test_cap_drops_whole_files_and_says_so(self):
        files = [{"filename": "a.py", "patch": PATCH}, {"filename": "b.py", "patch": PATCH}]
        block = reviewer._diff_block(files, cap=len(PATCH) + 40)
        self.assertIn("### `a.py`", block)
        self.assertNotIn("### `b.py`", block)
        self.assertIn("`b.py`", block)

    def test_triple_backticks_inside_patch_cannot_close_fence(self):
        block = reviewer._diff_block([{"filename": "a.md", "patch": "+```js\n+x\n+```"}])
        fences = [l for l in block.splitlines() if l.strip() in ("```", "```diff")]
        self.assertEqual(len(fences), 2)          # 여는 것과 닫는 것뿐
        self.assertNotIn("+```", block)           # patch 안의 것은 무력화됐다


if __name__ == "__main__":
    unittest.main()

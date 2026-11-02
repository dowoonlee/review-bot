"""PR 처리가 예외로 끝났을 때 찍는 로그 줄에 PR 이 들어가는지, head 변경은 실패로
찍히지 않는지 확인한다. 2026-11-02 에 #9105 의 head 변경 줄이 PR 없이 `실패:` 로만
찍혀 바로 아래 헤더인 #9088 의 에러로 읽혔다."""
from pathlib import Path
import runpy
import unittest

from lib import github, reviewer, worktree

BOT = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'review-bot'))
PR = {'repo': 'o/r', 'number': 9105, 'title': 't'}


class FailureLineTests(unittest.TestCase):
    def test_failure_line_names_the_pr_before_the_free_text(self):
        for exc in (github.GhError('gh 실행 실패: 401'), worktree.WorktreeError('checkout'),
                    reviewer.ReviewError('claude 실행 실패: timeout')):
            line = BOT['_failure_line'](PR, exc)
            head, _, detail = line.partition(' — ')
            self.assertEqual(head, '  실패 o/r#9105', line)
            self.assertEqual(detail, str(exc))

    def test_head_change_is_a_rereview_not_a_failure(self):
        exc = reviewer.HeadMovedError('실행 검증 중 base/head가 바뀌어 다시 리뷰해야 합니다.')
        self.assertIsInstance(exc, reviewer.ReviewError, 'ReviewError 를 잡는 자리에서 같이 잡혀야 한다')
        line = BOT['_failure_line'](PR, exc)
        head = line.split(' — ')[0]
        self.assertEqual(head, '  다시 리뷰 o/r#9105')
        self.assertNotIn('실패', head, '앱은 — 앞의 "실패" 로 빨간 줄을 정한다')

    def test_review_command_line_is_not_indented(self):
        line = BOT['_failure_line'](PR, reviewer.ReviewError('x'), indent='')
        self.assertTrue(line.startswith('실패 o/r#9105 — '), line)

    def test_long_messages_are_cut(self):
        line = BOT['_failure_line'](PR, reviewer.ReviewError('x' * 1000))
        self.assertLessEqual(len(line), len('  실패 o/r#9105 — ') + 200)

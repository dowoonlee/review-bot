"""게시·이벤트 변경이 틱 전체가 아니라 PR 하나만 잠그는지, 그리고 틱이 들고 있던
처리 이력 사본이 그 사이 게시된 기록을 덮지 않는지 확인한다."""
from contextlib import ExitStack
import json
from pathlib import Path
import runpy
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from lib import drafts, github, store


class PRLockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        stack = ExitStack(); self.addCleanup(stack.close)
        for name, path in [('STATE_DIR', self.root), ('DRAFTS', self.root / 'drafts'),
                           ('REVIEWED', self.root / 'reviewed.json'),
                           ('LOCK', self.root / 'tick.lock')]:
            stack.enter_context(mock.patch.object(store, name, str(path)))
        (self.root / 'drafts').mkdir()

    def test_pr_lock_is_exclusive_per_pr_and_released_on_exit(self):
        with store.pr_lock('o/r', 1) as first:
            self.assertTrue(first)
            with store.pr_lock('o/r', 1) as again:
                self.assertFalse(again)
            with store.pr_lock('o/r', 2) as other:
                self.assertTrue(other)
        with store.pr_lock('o/r', 1) as after:
            self.assertTrue(after)

    def test_pr_lock_survives_an_exception_in_the_body(self):
        with self.assertRaises(RuntimeError):
            with store.pr_lock('o/r', 1):
                raise RuntimeError('boom')
        with store.pr_lock('o/r', 1) as owned:
            self.assertTrue(owned)

    def test_commit_keeps_records_written_by_someone_else(self):
        store.save({'o/r#1': {'sha': 'a', 'posted': False}})
        tick_copy = store.load()                       # 틱이 처음에 들고 가는 사본
        store.save({'o/r#1': {'sha': 'a', 'posted': True}})  # 그 사이 publish 가 고침
        store.commit(tick_copy, lambda s: store.mark(s, 'o/r', 2, 'b', posted=False))
        saved = store.load()
        self.assertTrue(saved['o/r#1']['posted'], '게시 기록이 틱의 낡은 사본으로 덮였다')
        self.assertEqual(saved['o/r#2']['sha'], 'b')
        self.assertEqual(tick_copy, saved, '틱이 들고 있는 사본도 저장된 내용으로 갱신돼야 한다')

    def test_commit_returns_the_mutator_result_without_a_caller_copy(self):
        store.save({'o/r#1': {'sha': 'a'}})
        self.assertEqual(store.commit(None, lambda s: len(s)), 1)


class PublishLockTests(PRLockTests):
    def setUp(self):
        super().setUp()
        self.bot = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'review-bot'))
        self.logs = []
        patch = mock.patch.dict(self.bot['cmd_publish'].__globals__, {'_log': self.logs.append})
        patch.start(); self.addCleanup(patch.stop)

    def test_publish_does_not_wait_for_a_running_tick(self):
        args = SimpleNamespace(target='o/r#8132', dry_run=False)
        publish = mock.Mock(return_value=0)
        with mock.patch.object(store, 'acquire_lock', return_value=False), \
                mock.patch.dict(self.bot['cmd_publish'].__globals__, {'_publish': publish}):
            self.assertEqual(self.bot['cmd_publish'](args, {}), 0)
        publish.assert_called_once_with('o/r', 8132, {}, preview=False)

    def test_publish_refuses_while_the_bot_holds_that_pr(self):
        args = SimpleNamespace(target='o/r#8132', dry_run=False)
        publish = mock.Mock(return_value=0)
        with store.pr_lock('o/r', 8132), \
                mock.patch.dict(self.bot['cmd_publish'].__globals__, {'_publish': publish}):
            self.assertEqual(self.bot['cmd_publish'](args, {}), 1)
        publish.assert_not_called()
        self.assertTrue(any('처리하는 중' in line for line in self.logs))

    def test_event_change_refuses_only_for_the_locked_pr(self):
        for number in (1, 2):
            Path(store.draft_payload_path('o/r', number)).write_text(json.dumps(
                {'repo': 'o/r', 'number': number, 'sha': 'abc', 'event': 'COMMENT',
                 'reason': 'r', 'decision': {'judgeEvent': 'COMMENT', 'finalEvent': 'COMMENT',
                                             'summary': 'r', 'reasons': []}}))
        cfg = {'post': {'approveNeedsGreenCi': False}}
        with store.pr_lock('o/r', 1), mock.patch.object(store, 'acquire_lock', return_value=False), \
                mock.patch('builtins.print'):
            self.assertEqual(self.bot['cmd_draft_event'](SimpleNamespace(target='o/r#1', event='APPROVE'), cfg), 1)
            self.assertEqual(self.bot['cmd_draft_event'](SimpleNamespace(target='o/r#2', event='APPROVE'), cfg), 0)
        self.assertEqual(json.loads(Path(store.draft_payload_path('o/r', 1)).read_text())['event'], 'COMMENT')
        self.assertEqual(json.loads(Path(store.draft_payload_path('o/r', 2)).read_text())['event'], 'APPROVE')

    def test_reconcile_leaves_a_draft_someone_is_publishing(self):
        data = {'repo': 'o/r', 'number': 1, 'sha': 'abc', 'event': 'COMMENT'}
        Path(store.draft_payload_path('o/r', 1)).write_text(json.dumps(data))
        cfg = {'repos': {'o/r': {'account': 'work'}}}
        with store.pr_lock('o/r', 1), mock.patch.object(store, 'acquire_lock', return_value=True), \
                mock.patch.object(store, 'release_lock'), \
                mock.patch.object(github, '_gh_json', return_value={'state': 'MERGED'}):
            self.assertEqual(drafts.reconcile(cfg), [])
        self.assertEqual(len(store.pending_drafts()), 1)
        with mock.patch.object(store, 'acquire_lock', return_value=True), \
                mock.patch.object(store, 'release_lock'), \
                mock.patch.object(github, '_gh_json', return_value={'state': 'MERGED'}):
            drafts.reconcile(cfg)
        self.assertEqual(store.pending_drafts(), [])


if __name__ == '__main__':
    unittest.main()

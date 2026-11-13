"""봇이 바쁠 때 사람이 요청한 리뷰는 거절되지 않고 줄을 서며, 일하던 쪽이 이어서 본다."""
from contextlib import ExitStack
from pathlib import Path
import runpy
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from lib import queue, reviewer, store, updates

CFG = {'review': {'agents': ['a']}, 'post': {}, 'notify': {'enabled': False}}


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        stack = ExitStack(); self.addCleanup(stack.close)
        for name, path in [('STATE_DIR', self.root), ('DRAFTS', self.root / 'drafts'),
                           ('REVIEWED', self.root / 'reviewed.json'),
                           ('LOCK', self.root / 'tick.lock')]:
            stack.enter_context(mock.patch.object(store, name, str(path)))
        stack.enter_context(mock.patch.object(updates, 'pending', return_value=False))
        (self.root / 'drafts').mkdir()

    def order(self):
        return [(i['repo'], i['number']) for i in queue.items()]

    def test_requests_keep_their_order_and_one_place_per_pr(self):
        self.assertEqual(queue.add('o/r', 1), 1)
        self.assertEqual(queue.add('o/r', 2, {'rejudge': True, 'agents': None, 'unknown': 1}), 2)
        self.assertEqual(queue.add('o/r', 3), 3)
        # Asking again keeps the place and takes the newer options.
        self.assertEqual(queue.add('o/r', 2, {'agents': 'a'}), 2)
        self.assertEqual(self.order(), [('o/r', 1), ('o/r', 2), ('o/r', 3)])
        self.assertEqual(queue.items()[1]['options'], {'agents': 'a'})
        self.assertEqual(queue.add('o/r', 3, front=True), 1)
        self.assertTrue(queue.to_front('o/r', 2))
        self.assertEqual(self.order(), [('o/r', 2), ('o/r', 3), ('o/r', 1)])
        self.assertEqual(queue.first(skip={('o/r', 2)})['number'], 3)
        self.assertTrue(queue.remove('o/r', 3))
        self.assertFalse(queue.remove('o/r', 3))
        self.assertFalse(queue.to_front('o/r', 9))
        self.assertEqual(queue.clear(), 2)
        self.assertFalse(queue.pending())

    def test_a_damaged_file_is_an_empty_queue_not_a_crash(self):
        Path(queue.path()).write_text('{not json')
        self.assertEqual(queue.items(), [])
        self.assertEqual(queue.add('o/r', 1), 1)
        Path(queue.path()).write_text('[{"repo": "o/r"}, 3, {"repo": "o/r", "number": 2}]')
        self.assertEqual(self.order(), [('o/r', 2)])


class QueueCommandTests(QueueTests):
    def setUp(self):
        super().setUp()
        self.bot = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'review-bot'))
        self.globals = self.bot['cmd_review'].__globals__
        self.logs, self.reviewed = [], []

        def process(pr, cfg, state, force=False, mcp=None, force_size=False, rejudge=False):
            self.reviewed.append((pr['number'], rejudge, cfg['review']['agents']))
            if pr['number'] == 13:
                raise reviewer.ReviewError('boom')
            return {'repo': pr['repo'], 'number': pr['number'], 'sha': 'h', 'event': 'COMMENT', 'draft': True}
        patch = mock.patch.dict(self.globals, {
            '_log': self.logs.append, 'process_pr': process,
            '_connect_repomind': lambda cfg: (None, None),
            '_recover_resources': lambda cfg: None,
            '_narrow': lambda cfg, agents, note: dict(cfg, review={'agents': agents.split(',') if agents else ['a']})})
        patch.start(); self.addCleanup(patch.stop)

    def args(self, number, **extra):
        return SimpleNamespace(**dict({'target': 'o/r#%d' % number, 'rejudge': False, 'agents': None,
                                       'note': None, 'force_size': False, 'front': False}, **extra))

    def test_a_review_asked_for_while_the_bot_is_busy_is_queued_not_refused(self):
        with mock.patch.object(store, 'acquire_lock', return_value=False):
            self.assertEqual(self.bot['cmd_review'](self.args(1), CFG), 0)
            self.assertEqual(self.bot['cmd_review'](self.args(2, rejudge=True, agents='b'), CFG), 0)
            self.assertEqual(self.bot['cmd_review'](self.args(3, front=True), CFG), 0)
        self.assertEqual(self.order(), [('o/r', 3), ('o/r', 1), ('o/r', 2)])
        self.assertEqual(self.reviewed, [])
        self.assertIn('대기열에 넣었습니다 — 1번째', self.logs[0])

    def test_the_review_that_holds_the_lock_takes_the_queue_before_letting_go(self):
        queue.add('o/r', 2, {'rejudge': True, 'agents': 'b'})
        queue.add('o/r', 3)
        with mock.patch('builtins.print'):
            self.assertEqual(self.bot['cmd_review'](self.args(1), CFG), 0)
        # The request made now runs first, then the waiting ones in order with their own options.
        self.assertEqual(self.reviewed, [(1, False, ['a']), (2, True, ['b']), (3, False, ['a'])])
        self.assertEqual(queue.items(), [])
        self.assertEqual(set(store.load()), {'o/r#1', 'o/r#2', 'o/r#3'})
        self.assertFalse(Path(store.LOCK).exists())

    def test_a_request_that_arrives_as_the_lock_is_released_is_still_taken(self):
        real = store.release_lock
        late = []

        def release():
            real()
            if not late:
                late.append(queue.add('o/r', 7))   # nobody is looking at the queue at this moment
        with mock.patch.object(store, 'release_lock', side_effect=release), mock.patch('builtins.print'):
            self.bot['cmd_review'](self.args(1), CFG)
        self.assertEqual([n for n, _, _ in self.reviewed], [1, 7])
        self.assertEqual(queue.items(), [])

    def test_a_failed_request_leaves_the_queue_and_the_rest_still_run(self):
        queue.add('o/r', 13)
        queue.add('o/r', 4)
        self.assertEqual([r['number'] for r in self.bot['_drain_queue'](CFG, store.load(), {})], [4])
        self.assertEqual([n for n, _, _ in self.reviewed], [13, 4])
        self.assertEqual(queue.items(), [])

    def test_a_pr_held_by_another_command_keeps_its_place(self):
        queue.add('o/r', 5)
        queue.add('o/r', 6)
        with store.pr_lock('o/r', 5):
            self.bot['_drain_queue'](CFG, store.load(), {})
        self.assertEqual([n for n, _, _ in self.reviewed], [6])
        self.assertEqual(self.order(), [('o/r', 5)])

    def test_nothing_is_taken_while_an_update_is_scheduled_or_recon_is_down(self):
        queue.add('o/r', 5)
        with mock.patch.object(updates, 'pending', return_value=True):
            self.assertEqual(self.bot['_drain_queue'](CFG, store.load(), {}), [])
        self.assertEqual(self.bot['_drain_queue'](CFG, store.load(), {'mcp': None, 'blocked': 'down'}), [])
        self.assertEqual((self.reviewed, self.order()), ([], [('o/r', 5)]))

    def test_queue_command_removes_and_reorders(self):
        queue.add('o/r', 1); queue.add('o/r', 2)
        run = lambda action, target=None: self.bot['cmd_queue'](SimpleNamespace(action=action, target=target, json=False), CFG)
        self.assertEqual(run('front', 'o/r#2'), 0)
        self.assertEqual(self.order(), [('o/r', 2), ('o/r', 1)])
        self.assertEqual(run('remove', 'o/r#2'), 0)
        self.assertEqual(run('remove', 'o/r#2'), 1)
        self.assertEqual(run('remove'), 1)
        self.assertEqual(run('clear'), 0)
        self.assertEqual(queue.items(), [])


if __name__ == '__main__':
    unittest.main()

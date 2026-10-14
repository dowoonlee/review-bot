from contextlib import ExitStack
import json
from pathlib import Path
import runpy
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from lib import checkpoints, cleanup, store


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        stack = ExitStack(); self.addCleanup(stack.close)
        for name, path in [('STATE_DIR', self.root), ('DRAFTS', self.root/'drafts'),
                           ('REVIEWED', self.root/'reviewed.json')]:
            stack.enter_context(mock.patch.object(store, name, str(path)))
        (self.root/'drafts').mkdir()
        self.owned = self.root/'evidence/o_r/1/head/owned'
        self.legacy = self.root/'evidence/o_r/1/head/legacy'
        self.foreign = self.root/'evidence/o_r/1/head/foreign'
        for directory in (self.owned, self.legacy, self.foreign):
            directory.mkdir(parents=True)
            (directory/'screen.png').write_bytes(b'screen')
        (self.owned/'owner.json').write_text(json.dumps({'repo': 'o/r', 'number': 1}))
        (self.foreign/'owner.json').write_text(json.dumps({'repo': 'other/repo', 'number': 1}))
        self.target = {'repo': 'o/r', 'number': 1, 'sha': 'head', 'runtime': {
            'directory': str(self.owned), 'attempts': [{'directory': str(self.legacy)}, {'directory': str(self.foreign)}]}}
        other = {'repo': 'other/repo', 'number': 1, 'sha': 'other'}
        for name in ('runs.jsonl', 'verify.jsonl'):
            (self.root/name).write_text(json.dumps(self.target)+'\n'+json.dumps(other)+'\n{broken\n')
        self.draft = self.root/'drafts/o__r-1.json'
        self.draft.write_text(json.dumps(self.target))
        self.draft.with_suffix('.md').write_text('draft')
        store.save({'o/r#1': {'sha': 'head', 'posted': False, 'requestedAt': 'old'},
                    'other/repo#1': {'sha': 'other', 'posted': True}})
        checkpoints.save('o/r', 1, {'watchCI': True, 'payload': {}})

    def test_cleanup_preserves_other_owners_and_does_not_requeue_discarded_head(self):
        result = cleanup.clean_pr('o/r', 1)
        self.assertFalse(self.owned.exists())
        self.assertFalse(self.legacy.exists())
        self.assertTrue(self.foreign.exists())
        self.assertFalse(self.draft.exists())
        self.assertFalse(self.draft.with_suffix('.md').exists())
        self.assertIsNone(checkpoints.load('o/r', 1))
        self.assertEqual(result['records']['runs.jsonl'], 1)
        self.assertIn('{broken', (self.root/'runs.jsonl').read_text())
        self.assertIn('other/repo', (self.root/'runs.jsonl').read_text())
        state = store.load()
        self.assertTrue(store.already_done(state, 'o/r', 1, 'head', True, False, 'old'))
        self.assertFalse(store.already_done(state, 'o/r', 1, 'new-head', True, False, 'old'))
        self.assertFalse(store.already_done(state, 'o/r', 1, 'head', True, False, 'new-request'))
        self.assertEqual(state['other/repo#1'], {'sha': 'other', 'posted': True})

    def test_preview_changes_nothing_and_reset_is_explicit(self):
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        result = cleanup.clean_pr('o/r', 1, preview=True)
        self.assertTrue(result['artifacts'])
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        cleanup.clean_pr('o/r', 1, reset=True)
        self.assertNotIn('o/r#1', store.load())

    def test_artifact_symlinks_cannot_delete_external_directories(self):
        with tempfile.TemporaryDirectory() as outside:
            sentinel = Path(outside)/'keep'; sentinel.write_text('keep')
            (self.owned.parent/'linked').symlink_to(outside, target_is_directory=True)
            cleanup.clean_pr('o/r', 1)
            self.assertTrue(sentinel.exists())

    def test_busy_bot_refuses_cleanup(self):
        bot = runpy.run_path(str(Path(__file__).resolve().parents[1]/'review-bot'))
        args = SimpleNamespace(target='o/r#1', artifacts=True, dry_run=False, all=False,
                               at=None, older_than=None, reset=False)
        with mock.patch.object(store, 'acquire_lock', return_value=False), \
                mock.patch.dict(bot['cmd_forget'].__globals__, {'_log': lambda *_: None}):
            self.assertEqual(bot['cmd_forget'](args, {}), 1)
        self.assertTrue(self.draft.exists())

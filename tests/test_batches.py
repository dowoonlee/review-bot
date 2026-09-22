"""Large reviews preserve diff coverage, coordinates, and incomplete-run guards."""
import json
import os
import runpy
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from lib import batches, github, reviewer, worktree


def changed(name, count=4):
    return {'filename': name, 'status': 'added', 'additions': count, 'deletions': 0,
            'patch': '@@ -0,0 +1,%d @@\n' % count + ''.join('+line %d\n' % n for n in range(count))}


class PlanningTests(unittest.TestCase):
    def test_large_hunk_preserves_every_line_and_anchor(self):
        original = changed('large.py', 1500)
        groups, missing = batches.plan([original], max_files=20, max_chars=2000)
        self.assertGreater(len(groups), 1)
        self.assertEqual(missing, [])
        self.assertEqual(set().union(*(github.added_lines(f['patch']) for g in groups for f in g)),
                         github.added_lines(original['patch']))
        reconstructed = ''.join(line for g in groups for f in g for line in f['patch'].splitlines(True)
                                if line.startswith('+'))
        self.assertEqual(reconstructed, ''.join(original['patch'].splitlines(True)[1:]))
        for group in groups:
            self.assertLessEqual(sum(len(batches.render_file(f)) + 1 for f in group), 2000)

    def test_mixed_hunks_preserve_old_and_new_coordinates(self):
        patch = '@@ -10,200 +10,200 @@\n' + (' context\n-old\n+new\n' * 100)
        pieces = batches.split_patch(patch, 400)
        self.assertEqual(set().union(*(github.added_lines(p) for p in pieces)), github.added_lines(patch))
        self.assertEqual(set().union(*(github.postable_lines(p) for p in pieces)), github.postable_lines(patch))

    def test_file_cap_and_missing_diff_are_explicit(self):
        files = [changed('file-%03d.py' % i) for i in range(137)]
        files.append({'filename': 'image.png'})
        groups, missing = batches.plan(files, max_files=20)
        self.assertEqual(len(groups), 7)
        self.assertEqual({f['filename'] for g in groups for f in g}, {f['filename'] for f in files})
        self.assertEqual(missing[0]['path'], 'image.png')
        self.assertTrue(all(len(g) <= 20 for g in groups))

    def test_indivisible_line_is_reported_not_silently_reviewed(self):
        groups, missing = batches.plan([dict(changed('minified.js'), patch='@@ -0,0 +1 @@\n+' + 'x' * 3000)],
                                       max_chars=2000)
        self.assertEqual(groups[0][0]['patch'], '')
        self.assertEqual(missing[0]['path'], 'minified.js')


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.meta = {'_repo': 'o/r', '_number': 1, 'headRefOid': 'head'}
        self.roles = mock.patch.object(reviewer.agents_mod, 'role', return_value='role')
        self.roles.start()
        self.addCleanup(self.roles.stop)

    def review(self, files, run, **kwargs):
        with mock.patch.object(reviewer, '_run', side_effect=run):
            return reviewer.review(self.meta, files, ['code', 'security'], cwd=self.tmp.name,
                                   parallel=False, batching={'batchMaxFiles': 1}, **kwargs)

    def test_each_role_sees_every_batch_then_cross_batch_material(self):
        calls = []
        def run(prompt, cwd, model, turns, timeout, tag, **kwargs):
            calls.append(tag)
            material = next(Path(cwd).glob('.review-bot-material-*'))
            manifest = (material / 'manifest.md').read_text()
            self.assertIn('api.py', manifest)
            self.assertIn('consumer.py', manifest)
            if tag.startswith('integration'):
                index = json.loads((material / 'results.json').read_text())
                self.assertEqual(len(index['reports']), 4)
                for path in index['reports']:
                    self.assertIn('summary', json.loads(Path(path).read_text()))
                self.assertIn('직렬화/파서', prompt)
            return {'summary': tag, 'findings': [{'path': 'api.py', 'line': 1, 'title': 'same',
                    'severity': 'suggestion', 'body': 'evidence'}], '_meta': {'cost_usd': 1}}
        result = self.review([changed('api.py'), changed('consumer.py')], run)
        self.assertEqual(len(calls), 5)
        self.assertTrue(calls[-1].startswith('integration'))
        self.assertEqual(len(result['findings']), 1)
        self.assertEqual(result['_meta']['cost_usd'], 5)
        self.assertTrue(result['coverage']['complete'])
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])

    def test_failed_batch_and_integration_keep_findings_but_block_approval(self):
        def run(prompt, cwd, model, turns, timeout, tag, **kwargs):
            if tag in ('batch-001-code', 'integration-code'):
                raise reviewer.ReviewError('timeout', cost=2)
            return {'findings': [], '_meta': {'cost_usd': 1}}
        result = self.review([changed('a.py'), changed('b.py')], run)
        self.assertEqual(result['coverage']['completed'], 3)
        self.assertEqual(result['coverage']['integrationCompleted'], 0)
        self.assertFalse(result['coverage']['complete'])
        self.assertEqual(result['_meta']['cost_usd'], 7)
        bot = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'review-bot'))
        event, reason = bot['_guard_event']('APPROVE', [], result, {'allowedEvents': ['APPROVE']})
        self.assertEqual(event, 'COMMENT')
        self.assertIn('미완료', reason)
        self.assertIn('3/4', bot['_summary_body'](result, [], []))

    def test_small_pr_has_no_integration_and_binary_cannot_be_approved(self):
        result = self.review([{'filename': 'image.png'}], lambda *a, **kw: {'findings': []})
        self.assertEqual(result['coverage']['integrationPlanned'], 0)
        self.assertFalse(result['coverage']['complete'])

    def test_all_failures_preserve_cost(self):
        def fail(*args, **kwargs):
            raise reviewer.ReviewError('bad', cost=3)
        with self.assertRaises(reviewer.ReviewError) as caught:
            self.review([changed('a.py')], fail)
        self.assertEqual(caught.exception.cost, 6)

    def test_large_verification_includes_anchor_and_full_diff(self):
        patch = changed('large.py', 30000)['patch']
        with reviewer.verification_diff(patch, 28000, self.tmp.name) as block:
            self.assertIn('+line 27999', block)
            self.assertLess(len(block), 121000)
            artifact = next(Path(self.tmp.name).glob('.review-bot-verify-*/full.diff'))
            self.assertEqual(artifact.read_text(), patch)
        self.assertFalse(artifact.exists())


class RecoveryTests(unittest.TestCase):
    def test_missing_and_truncated_patches_are_recovered_from_pinned_git(self):
        import subprocess
        with tempfile.TemporaryDirectory() as directory:
            def git(*args):
                return subprocess.check_output(['git', '-C', directory, '-c', 'core.hooksPath=/dev/null',
                    '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', *args],
                    stderr=subprocess.DEVNULL, text=True).strip()
            git('init')
            path = Path(directory) / 'data.py'
            path.write_text('base\n')
            git('add', '.')
            git('commit', '-m', 'base')
            base = git('rev-parse', 'HEAD')
            path.write_text('base\nnew\n')
            git('add', '.')
            git('commit', '-m', 'head')
            head = git('rev-parse', 'HEAD')
            item = dict(filename='data.py', additions=1, deletions=0)
            for patch in (None, '@@ -1 +1 @@\n base'):
                with self.subTest(patch=patch):
                    recovered = worktree.complete_patches(directory, head, base, [dict(item, patch=patch)])
                    self.assertEqual(github.added_lines(recovered[0]['patch']), {2})
                    self.assertIn('+new', recovered[0]['patch'])
            with self.assertRaises(worktree.WorktreeError):
                worktree.complete_patches(directory, head, base, [dict(item, additions=9)])

    def test_verifier_uses_original_anchor_after_large_deleted_range(self):
        patch = '@@ -1,30000 +1,2 @@\n' + '-old line\n' * 30000 + '+first\n+second\n'
        with tempfile.TemporaryDirectory() as directory:
            with reviewer.verification_diff(patch, 2, directory) as block:
                self.assertIn('+second', block)
                self.assertLess(len(block), 121000)


class ConcurrencyTests(unittest.TestCase):
    def test_parallelism_is_bounded_across_all_batches(self):
        import time
        active = maximum = 0
        lock = threading.Lock()
        def run(*args, **kwargs):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(.01)
            with lock:
                active -= 1
            return {'findings': []}
        with tempfile.TemporaryDirectory() as cwd, mock.patch.object(reviewer.agents_mod, 'role', return_value='role'), \
                mock.patch.object(reviewer, '_run', side_effect=run):
            result = reviewer.review({'_repo': 'o/r', '_number': 1}, [changed(str(n)) for n in range(6)],
                                     ['code', 'security', 'failure'], cwd, parallel=True,
                                     batching={'batchMaxFiles': 1, 'workers': 2})
        self.assertEqual(maximum, 2)
        self.assertEqual(result['coverage']['completed'], 18)
        self.assertEqual(result['coverage']['integrationCompleted'], 1)


class SnapshotGuardTests(unittest.TestCase):
    def test_incomplete_file_list_and_changed_checkout_never_start_review(self):
        from contextlib import ExitStack
        from copy import deepcopy
        from lib import config, store
        bot = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'review-bot'))
        cfg = deepcopy(config.DEFAULTS)
        cfg['repos'] = {'o/r': {'enabled': True}}
        for count, checkout in ((2, 'head'), (1, 'new-head')):
            with self.subTest(count=count, checkout=checkout), ExitStack() as stack:
                stack.enter_context(mock.patch.object(github, 'active_account', return_value='me'))
                stack.enter_context(mock.patch.object(github, 'env_for', return_value={}))
                stack.enter_context(mock.patch.object(github, 'pr_meta', return_value={
                    'state': 'OPEN', 'headRefOid': 'head', 'changedFiles': count}))
                stack.enter_context(mock.patch.object(github, 'pr_files', return_value=[changed('a.py')]))
                stack.enter_context(mock.patch.object(github, 'review_requester', return_value=None))
                stack.enter_context(mock.patch.object(github, 'last_review_request', return_value=None))
                stack.enter_context(mock.patch.object(store, 'set_current'))
                stack.enter_context(mock.patch.object(store, 'clear_current'))
                stack.enter_context(mock.patch.object(worktree, 'prepare', return_value=('/wt', checkout, '/base', 'ref')))
                stack.enter_context(mock.patch.object(worktree, 'cleanup'))
                stack.enter_context(mock.patch.dict(bot['process_pr'].__globals__, {'_log': lambda _: None}))
                reviewed = stack.enter_context(mock.patch.object(reviewer, 'review'))
                posted = stack.enter_context(mock.patch.object(github, 'post_review'))
                with self.assertRaises(reviewer.ReviewError):
                    bot['process_pr']({'repo': 'o/r', 'number': 1}, cfg, {})
                reviewed.assert_not_called()
                posted.assert_not_called()


class IntegrationModeTests(unittest.TestCase):
    def test_all_mode_retains_independent_integration_roles_and_timing(self):
        with tempfile.TemporaryDirectory() as cwd, mock.patch.object(reviewer.agents_mod, 'role', return_value='role'), \
                mock.patch.object(reviewer, '_run', return_value={'findings': []}):
            result = reviewer.review({'_repo': 'o/r', '_number': 1}, [changed('a'), changed('b')],
                                     ['code', 'security'], cwd, parallel=True,
                                     batching={'batchMaxFiles': 1, 'integrationMode': 'all'})
        self.assertEqual(result['coverage']['integrationPlanned'], 2)
        self.assertEqual(result['coverage']['integrationCompleted'], 2)
        self.assertTrue(result['coverage']['complete'])
        self.assertEqual(result['_meta']['workers'], 2)
        self.assertTrue(all('duration_sec' in value for value in result['_meta']['agents'].values()))

    def test_invalid_mode_does_not_silently_skip_integration(self):
        with self.assertRaises(reviewer.ReviewError):
            reviewer.review({}, [changed('a')], ['code'], '.', batching={'integrationMode': 'invalid'})

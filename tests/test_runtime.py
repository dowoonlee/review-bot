"""Execution evidence must reach reviewers and must not turn missing tests green."""
import copy
import json
import os
import time
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest import mock

from lib import github, janitor, reviewer, runtime


META = {'_repo': 'owner/repo', '_number': 7, 'headRefOid': 'abc', 'title': 'UI'}
PLAN = {'testsApplicable': True, 'visualApplicable': True, 'setup': 'npm ci',
        'tests': [{'name': 'unit', 'command': 'npm test'}],
        'start': 'npm run dev', 'browser': "await capture('changed');",
        'scenarios': ['hover and click state'], 'limitations': []}
GOOD = {'status': 'complete', 'testsApplicable': True, 'visualApplicable': True,
        'tests': [{'status': 'passed'}], 'screenshots': ['screen.png'],
        'visual': {'status': 'passed', 'complete': True}, 'limitations': []}


class EvidenceTests(unittest.TestCase):
    def test_no_ci_can_approve_with_real_local_evidence_but_missing_evidence_cannot(self):
        guard = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'review-bot'))['_guard_event']
        cfg = {'allowedEvents': ['COMMENT', 'APPROVE']}
        self.assertEqual(guard('APPROVE', [], {'runtime': GOOD}, cfg, 'none'), ('APPROVE', ''))
        for change in ({'status': 'incomplete'}, {'tests': []},
                       {'tests': [{'status': 'failed'}]}, {'screenshots': []},
                       {'visual': {'status': 'passed', 'complete': False}},
                       {'limitations': ['theme not checked']}):
            with self.subTest(change=change):
                event, reason = guard('APPROVE', [], {'runtime': dict(GOOD, **change)}, cfg, 'none')
                self.assertEqual(event, 'COMMENT')
                self.assertTrue(reason)
        self.assertEqual(guard('APPROVE', [], {'runtime': GOOD}, cfg, 'pending')[0], 'COMMENT')

    def test_ci_expected_and_skipped_are_not_passes_and_links_survive(self):
        for conclusion, expected in [('SKIPPED', 'none'), ('NEUTRAL', 'none'), ('EXPECTED', 'pending')]:
            self.assertEqual(github.ci_state({'statusCheckRollup': [{'conclusion': conclusion}]}), expected)
        meta = dict(META, statusCheckRollup=[{'name': 'vitest', 'status': 'COMPLETED',
                                             'conclusion': 'SUCCESS', 'detailsUrl': 'https://example/check/1'}])
        result = github.ci_evidence(meta)
        self.assertEqual(result['sha'], 'abc')
        self.assertEqual(result['checks'][0]['url'], 'https://example/check/1')

    def test_both_review_and_judge_receive_execution_evidence(self):
        meta = dict(META, _runtime=GOOD, _ci_evidence={'state': 'none', 'checks': []})
        with mock.patch.object(reviewer.agents_mod, 'role', return_value='role'):
            prompt = reviewer.build_prompt('code', meta, [])
        self.assertIn('screen.png', prompt)
        self.assertIn('passed', prompt)
        with mock.patch.object(reviewer, '_run', return_value={'event': 'APPROVE'}) as run:
            reviewer.judge(meta, {'summary': 'traced consumers'}, [], ['APPROVE'])
        self.assertIn('screen.png', run.call_args.args[0])
        self.assertIn('로컬 테스트', run.call_args.args[0])

    def test_visual_inspector_cannot_pass_without_reading_all_images(self):
        with mock.patch.object(reviewer, '_run', return_value={'passed': True, 'complete': True, 'inspected': []}):
            result = runtime._inspect(META, PLAN, GOOD, '/tmp', {})
        self.assertFalse(result['complete'])


class RetentionTests(unittest.TestCase):
    def test_only_finished_old_runs_are_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp, 'evidence/repo/1/sha')
            for name in ('old', 'recent', 'active'):
                (root / name).mkdir(parents=True)
            for name in ('old', 'recent'):
                (root / name / 'result.json').write_text('{}')
            old = time.time() - 20 * 86400
            os.utime(root / 'old/result.json', (old, old))
            with mock.patch.object(janitor, 'STATE_DIR', tmp):
                self.assertEqual(janitor.prune_evidence(), 1)
            self.assertFalse((root / 'old').exists())
            self.assertTrue((root / 'recent/result.json').exists())
            self.assertTrue((root / 'active').exists())


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patch = mock.patch.object(reviewer, 'STATE_DIR', self.temp.name)
        patch.start()
        self.addCleanup(patch.stop)

    def test_missing_docker_is_incomplete_and_durable(self):
        with mock.patch.object(runtime, '_plan', return_value=copy.deepcopy(PLAN)), \
                mock.patch.object(runtime.Container, 'command', side_effect=OSError('no docker')):
            result = runtime.collect(META, [], self.temp.name, {})
        self.assertEqual(result['status'], 'incomplete')
        self.assertTrue(runtime.approval_block(result))
        persisted = json.loads(Path(result['directory'], 'result.json').read_text())
        self.assertIn('no docker', persisted['limitations'][0])
        runtime._LIVE_CONTAINERS.clear()

    def test_docs_plan_does_not_start_container(self):
        plan = dict(PLAN, testsApplicable=False, visualApplicable=False, tests=[])
        with mock.patch.object(runtime, '_plan', return_value=plan), \
                mock.patch.object(runtime, 'Container') as container:
            result = runtime.collect(META, [], self.temp.name, {})
        container.assert_not_called()
        self.assertEqual(runtime.approval_block(result), '')

    def test_failed_test_is_not_hidden_by_successful_browser_and_cleanup_runs(self):
        commands = []
        def command(args, *a, **kw):
            commands.append(args)
            if args[0] == 'git':
                Path(next(arg[9:] for arg in args if arg.startswith('--output='))).touch()
            return {'status': 'passed', 'exitCode': 0, 'output': ''}
        def shell(script, *a, **kw):
            return {'status': 'failed' if script == 'npm test' else 'passed', 'exitCode': 1 if script == 'npm test' else 0}
        with mock.patch.object(runtime, '_plan', return_value=copy.deepcopy(PLAN)), \
                mock.patch.object(runtime.Container, 'command', side_effect=command), \
                mock.patch.object(runtime.Container, 'shell', side_effect=shell), \
                mock.patch.object(runtime, '_screenshots', return_value=['/tmp/screen.png']), \
                mock.patch.object(runtime, '_inspect', return_value={'status': 'passed', 'complete': True, 'cost_usd': 0}):
            result = runtime.collect(META, [], self.temp.name, {})
        self.assertTrue(runtime.approval_block(result))
        self.assertEqual(commands[-1][:4], ['docker', 'rm', '-f', '-v'])
        create = next(c for c in commands if c[:2] == ['docker', 'create'])
        self.assertIn('--cap-drop=ALL', create)
        self.assertNotIn('-v', create)
        self.assertNotIn('-p', create)
        self.assertTrue(any(c[:3] == ['docker', 'network', 'disconnect'] for c in commands))

    def test_browser_timeout_still_removes_container(self):
        def command(args, *a, **kw):
            if args[0] == 'git':
                Path(next(arg[9:] for arg in args if arg.startswith('--output='))).touch()
            return {'status': 'passed', 'exitCode': 0, 'output': ''}
        def shell(script, *a, **kw):
            if script.startswith('node '):
                raise runtime.ExecutionError('browser timeout')
            return {'status': 'passed', 'exitCode': 0}
        with mock.patch.object(runtime, '_plan', return_value=copy.deepcopy(PLAN)), \
                mock.patch.object(runtime.Container, 'command', side_effect=command) as run, \
                mock.patch.object(runtime.Container, 'shell', side_effect=shell):
            result = runtime.collect(META, [], self.temp.name, {})
        self.assertEqual(result['status'], 'incomplete')
        self.assertEqual(run.call_args.args[0][:4], ['docker', 'rm', '-f', '-v'])
        self.assertIn('browser timeout', result['limitations'])


if __name__ == '__main__':
    unittest.main()

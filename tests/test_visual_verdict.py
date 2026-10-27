"""Screen verification judges the screen; unproven states are replanned, not left ambiguous."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib import reviewer, runtime, validation


META = {'_repo': 'o/r', '_number': 1, 'headRefOid': 'head'}
LAUNCH = ("browserType.launch: Executable doesn't exist at /ms-playwright/chromium_headless_shell-1208/"
          "chrome-linux/headless_shell")


class BrowserInfrastructureTests(unittest.TestCase):
    def test_image_browsers_survive_the_repositorys_playwright_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            container = runtime.Container(tmp, {})
            seen = []

            def command(args, *a, **kw):
                seen.append(args)
                if args[0] == 'git':
                    Path(next(arg[9:] for arg in args if arg.startswith('--output='))).touch()
                return {'status': 'passed', 'exitCode': 0, 'output': ''}
            plan = {'setup': 'pnpm exec playwright install chromium', 'services': []}
            with mock.patch.object(container, 'command', side_effect=command):
                runtime._provision(container, 'head', tmp, {}, plan, True)
        create = next(a for a in seen if a[:2] == ['docker', 'create'])
        self.assertIn('PLAYWRIGHT_SKIP_BROWSER_GC=1', create)

    def test_a_browser_that_cannot_start_is_recovered_not_blamed_on_the_product(self):
        self.assertEqual(validation.failure_kind({'status': 'failed', 'output': LAUNCH}, browser=True),
                         'infrastructure')
        self.assertEqual(validation.failure_kind({'status': 'failed', 'output': 'AssertionError: 2 != 3'},
                                                 browser=True), 'test')


class InspectionTests(unittest.TestCase):
    def inspect(self, answer):
        plan = {'scenarios': ['limit row'], 'browser': "await capture('x')", 'start': '', 'tests': [{'command': 'pytest'}]}
        evidence = {'screenshots': ['/tmp/01.png'], 'browser': {'status': 'passed'},
                    'tests': [{'name': 'backend', 'status': 'failed', 'output': 'exit 1'}]}
        with mock.patch.object(reviewer, '_run', return_value=dict(answer, inspected=['/tmp/01.png'])) as run:
            result = runtime._inspect(META, plan, evidence, '/tmp', {})
        return result, run.call_args.args[0]

    def test_unrelated_test_results_are_not_given_to_the_screen_judge(self):
        _, prompt = self.inspect({'passed': True, 'complete': True})
        self.assertNotIn('"backend"', prompt)
        self.assertNotIn('exit 1', prompt)
        self.assertIn('limit row', prompt)

    def test_missing_states_are_kept_only_when_no_defect_was_seen(self):
        result, _ = self.inspect({'passed': True, 'complete': False, 'missing': ['limit row not on screen']})
        self.assertEqual((result['status'], result['missing']), ('incomplete', ['limit row not on screen']))
        result, _ = self.inspect({'passed': False, 'complete': False, 'missing': ['limit row not on screen']})
        self.assertEqual(result['missing'], [])
        result, _ = self.inspect({'passed': True, 'complete': True, 'missing': ['ignored']})
        self.assertEqual((result['status'], result['missing']), ('passed', []))


class RecoveryTests(unittest.TestCase):
    def test_an_evidence_gap_replans_the_browser_with_the_missing_states(self):
        gap = {'status': 'complete', 'tests': [], 'limitations': [], 'screenshots': ['01.png'],
               'browser': {'status': 'passed'},
               'visual': {'status': 'incomplete', 'missing': ['limit row not on screen']}}
        done = dict(gap, visual={'status': 'passed', 'complete': True, 'missing': []})
        with mock.patch.object(runtime, '_collect_once', side_effect=[gap, done]) as collect:
            result = runtime.collect(META, [], '/tmp', {})
        self.assertEqual(collect.call_count, 2)
        self.assertIn('limit row not on screen', collect.call_args.args[0]['_runtime_retry']['failures'])
        self.assertEqual(result['visual']['status'], 'passed')

    def test_a_product_defect_on_screen_is_not_replanned(self):
        defect = {'status': 'complete', 'tests': [], 'limitations': [], 'screenshots': ['01.png'],
                  'browser': {'status': 'passed'}, 'visual': {'status': 'incomplete', 'missing': []}}
        with mock.patch.object(runtime, '_collect_once', return_value=defect) as collect:
            runtime.collect(META, [], '/tmp', {})
        self.assertEqual(collect.call_count, 1)


if __name__ == '__main__':
    unittest.main()

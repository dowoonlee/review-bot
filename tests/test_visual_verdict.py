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
        # The repository's own Playwright browsers are installed after setup, while
        # the network is still connected.
        scripts = [a[-1] for a in seen]
        install = scripts.index(runtime.PLAYWRIGHT_BROWSERS)
        self.assertGreater(install, scripts.index('pnpm exec playwright install chromium'))
        disconnect = next(i for i, a in enumerate(seen) if a[:3] == ['docker', 'network', 'disconnect'])
        self.assertLess(install, disconnect)

    def test_a_browser_that_cannot_start_is_recovered_not_blamed_on_the_product(self):
        self.assertEqual(validation.failure_kind({'status': 'failed', 'output': LAUNCH}, browser=True),
                         'infrastructure')
        self.assertEqual(validation.failure_kind({'status': 'failed', 'output': 'AssertionError: 2 != 3'},
                                                 browser=True), 'test')
        # The repository's own e2e runner without its browser is not a product failure either.
        missing = {'status': 'failed', 'message': 'Error: ' + LAUNCH}
        self.assertEqual(validation.failure_kind({'status': 'failed', 'cases': [missing, missing]}), 'infrastructure')
        self.assertEqual(validation.failure_kind({'status': 'failed', 'cases': [
            missing, {'status': 'failed', 'message': 'expect(locator).toBeVisible() failed'}]}), 'test')


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


class TriageTests(unittest.TestCase):
    EVIDENCE = {'screenshots': ['/tmp/01.png', '/tmp/02.png'],
                'browser': {'status': 'failed', 'output': "AssertionError: h1 스타일 적용 actual: ''"}}

    def triage(self, answer):
        plan = {'scenarios': ['h1 keeps display size'], 'browser': 'assert.ok(fs)'}
        with mock.patch.object(reviewer, '_run', return_value=answer):
            return runtime._triage(META, plan, self.EVIDENCE, '/tmp', {})

    def test_a_wrong_script_is_cleared_only_with_screen_evidence_from_the_failure_screen(self):
        script = {'cause': 'script', 'summary': 'read during re-render', 'screenEvidence': 'h1 visible at 600px',
                  'repair': 'wait for layout', 'inspected': ['/tmp/01.png', '/tmp/02.png']}
        self.assertEqual(self.triage(script)['cause'], 'script')
        for change in ({'screenEvidence': ''}, {'inspected': ['/tmp/01.png']}, {'cause': 'unsure'},
                       {'_meta': {'salvaged': True}}):
            with self.subTest(change=change):
                result = self.triage(dict(script, **change))
                self.assertEqual((result['cause'], result['repair']), ('defect', ''))

    def test_failed_assertion_is_replanned_only_when_cleared(self):
        failed = {'status': 'complete', 'tests': [], 'limitations': [], 'screenshots': ['01.png'],
                  'browser': {'status': 'failed', 'output': 'AssertionError', 'failureKind': 'plan',
                              'triage': {'cause': 'script', 'screenEvidence': 'h1 visible', 'repair': 'wait'}}}
        fixed = dict(failed, browser={'status': 'passed'}, visual={'status': 'passed', 'complete': True})
        with mock.patch.object(runtime, '_collect_once', side_effect=[failed, fixed]) as collect:
            runtime.collect(META, [], '/tmp', {})
        self.assertEqual(collect.call_count, 2)
        self.assertIn('h1 visible', collect.call_args.args[0]['_runtime_retry']['failures'])
        defect = dict(failed, browser=dict(failed['browser'], failureKind='test', triage={'cause': 'defect'}))
        with mock.patch.object(runtime, '_collect_once', return_value=defect) as collect:
            runtime.collect(META, [], '/tmp', {})
        self.assertEqual(collect.call_count, 1)


class BrowserBaselineTests(unittest.TestCase):
    HEAD = ['/tmp/h/01.png', '/tmp/h/02.png']
    BASE = ['/tmp/b/01.png', '/tmp/b/02.png']

    def evidence(self, browser='passed', visual=None):
        return {'directory': tempfile.mkdtemp(), 'cost_usd': 0, 'limitations': [], 'screenshots': list(self.HEAD),
                'browser': {'status': browser, 'output': 'ok', **({} if browser == 'passed' else {'failureKind': 'test'})},
                'visual': visual or {'status': 'incomplete', 'complete': False, 'summary': '05 empty but assert passed',
                                     'missing': []}}

    def run_baseline(self, evidence, answer):
        plan = {'scenarios': ['draft kept at 930px'], 'browser': '', 'start': '', 'setup': '', 'services': []}
        base_browser = {'status': 'passed', 'output': 'ok'}
        with mock.patch.object(runtime, '_merge_base', return_value='basesha'), \
                mock.patch.object(runtime, '_provision'), \
                mock.patch.object(runtime.Container, 'close'), \
                mock.patch.object(runtime, '_run_browser', return_value=(base_browser, list(self.BASE))), \
                mock.patch.object(reviewer, '_run', return_value=answer):
            runtime._browser_baseline(META, plan, evidence, '/tmp', {}, None, lambda *_: None)
        return evidence

    def test_only_an_unresolved_verdict_that_replanning_cannot_fix_is_compared(self):
        self.assertTrue(runtime._needs_browser_baseline(self.evidence()))
        self.assertFalse(runtime._needs_browser_baseline(self.evidence(visual={'status': 'passed'})))
        self.assertFalse(runtime._needs_browser_baseline(self.evidence(visual={'status': 'incomplete', 'missing': ['x']})))
        replanned = self.evidence(browser='failed')
        replanned['browser']['failureKind'] = 'plan'
        self.assertFalse(runtime._needs_browser_baseline(replanned))

    def test_behaviour_identical_on_base_clears_the_screen_verdict(self):
        seen = self.HEAD + self.BASE
        result = self.run_baseline(self.evidence(), {'verdict': 'same', 'summary': 'base shows the same empty input',
                                                     'inspected': seen})
        self.assertEqual((result['visual']['status'], result['visual']['complete']), ('passed', True))
        self.assertEqual(result['browserBaseline']['verdict'], 'same')
        self.assertIn('base shows the same empty input', result['visual']['summary'])

    def test_same_needs_every_screenshot_on_both_sides_and_a_completed_head_run(self):
        for answer, browser in (({'verdict': 'same', 'summary': 's', 'inspected': self.HEAD}, 'passed'),
                                ({'verdict': 'same', 'summary': 's', 'inspected': self.HEAD + self.BASE}, 'failed'),
                                ({'verdict': 'differs', 'summary': 'only head', 'inspected': self.HEAD + self.BASE}, 'passed'),
                                ({'verdict': 'unsure', 'summary': '?', 'inspected': self.HEAD + self.BASE}, 'passed')):
            with self.subTest(answer=answer, browser=browser):
                result = self.run_baseline(self.evidence(browser=browser), answer)
                self.assertEqual(result['visual']['status'], 'incomplete')
        result = self.run_baseline(self.evidence(), {'verdict': 'unsure', 'inspected': self.HEAD + self.BASE})
        self.assertEqual(result['browserBaseline']['verdict'], 'differs')


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

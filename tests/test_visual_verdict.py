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


class RepositoryToolchainTests(unittest.TestCase):
    """The repository decides the Playwright version; installs are bounded and retried."""

    def provision(self, tmp, sha, outcomes=None, shell_error=None, cfg=None):
        container = runtime.Container(tmp, {})
        seen = []
        outcomes = outcomes or {}

        def command(args, *a, **kw):
            seen.append(args)
            if args[0] == 'git' and 'archive' in args:
                Path(next(arg[9:] for arg in args if arg.startswith('--output='))).touch()
            for key, outcome in outcomes.items():
                if args[:len(key)] == list(key):
                    if isinstance(outcome, Exception):
                        raise outcome
                    return dict({'status': 'passed', 'exitCode': 0, 'output': ''}, **outcome)
            if shell_error and args[-1] == runtime.PLAYWRIGHT_BROWSERS:
                raise shell_error
            return {'status': 'passed', 'exitCode': 0, 'output': ''}
        with mock.patch.object(container, 'command', side_effect=command), \
                mock.patch.object(runtime.time, 'sleep') as sleep:
            evidence = runtime._provision(container, sha, tmp, cfg or {}, {'setup': '', 'services': []}, True)
        return evidence, seen, sleep

    def repository(self, tmp):
        def git(*args):
            return runpy_git(tmp, *args)
        git('init', '-q')
        Path(tmp, 'web').mkdir()
        Path(tmp, 'web/pnpm-lock.yaml').write_text(
            "packages:\n\n  '@playwright/test@1.61.0':\n    resolution: {}\n\n  playwright-core@1.61.0:\n"
            "    resolution: {}\n\n  playwright@1.61.0:\n    resolution: {}\n")
        Path(tmp, 'desktop').mkdir()
        Path(tmp, 'desktop/package-lock.json').write_text(
            '{"packages": {"node_modules/playwright": {"version": "1.59.1"}}}')
        git('add', '.')
        git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'lock')
        return git('rev-parse', 'HEAD').strip()

    def test_image_and_driver_follow_the_repositorys_newest_pinned_playwright(self):
        with tempfile.TemporaryDirectory() as tmp:
            sha = self.repository(tmp)
            self.assertEqual(runtime.repository_playwright(tmp, sha), '1.61.0')
            evidence, seen, _ = self.provision(tmp, sha, {('docker', 'image', 'inspect'): {'exitCode': 1}})
        image = 'mcr.microsoft.com/playwright:v1.61.0-noble'
        self.assertEqual(evidence['image'], {'image': image, 'playwright': '1.61.0', 'source': 'repository'})
        self.assertIn(['docker', 'pull', image], seen)
        self.assertIn(image, next(a for a in seen if a[:2] == ['docker', 'create']))
        self.assertTrue(any(a[-1].endswith('playwright@1.61.0') for a in seen if a[:2] == ['docker', 'exec']))

    def test_a_stalled_pull_is_retried_after_a_pause_then_falls_back_to_the_configured_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            sha = self.repository(tmp)
            evidence, seen, sleep = self.provision(tmp, sha, {
                ('docker', 'image', 'inspect'): {'exitCode': 1},
                ('docker', 'pull'): runtime.ExecutionError('시간 초과: 002.log — 이미지 받기 (900s)')})
        self.assertEqual(sum(1 for a in seen if a[:2] == ['docker', 'pull']), 3)
        self.assertEqual([c.args for c in sleep.call_args_list], [(30,), (30,)])
        self.assertEqual(evidence['image']['source'], 'config')
        self.assertEqual(evidence['image']['repositoryPlaywright'], '1.61.0')
        self.assertIn(runtime.DEFAULT_IMAGE, next(a for a in seen if a[:2] == ['docker', 'create']))
        self.assertTrue(any(a[-1].endswith('playwright@' + runtime.BROWSER_VERSION) for a in seen))

    def test_a_release_without_an_image_is_not_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            sha = self.repository(tmp)
            _, seen, sleep = self.provision(tmp, sha, {
                ('docker', 'image', 'inspect'): {'exitCode': 1},
                ('docker', 'pull'): {'exitCode': 1, 'output': 'manifest unknown'}})
        self.assertEqual(sum(1 for a in seen if a[:2] == ['docker', 'pull']), 1)
        sleep.assert_not_called()

    def test_a_browser_install_that_outlives_its_retries_does_not_abort_the_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence, seen, _ = self.provision(tmp, 'head', shell_error=runtime.ExecutionError('시간 초과'))
        self.assertEqual(evidence['playwrightBrowsers']['status'], 'failed')
        self.assertTrue(any(a[:3] == ['docker', 'network', 'disconnect'] for a in seen))
        install = next(a for a in seen if a[-1] == runtime.PLAYWRIGHT_BROWSERS)
        for env in ('REVIEW_BROWSER_ATTEMPTS=3', 'REVIEW_BROWSER_TIMEOUT=300', 'REVIEW_BROWSER_RETRY_DELAY=30'):
            self.assertIn(env, install)

    def test_timeouts_name_the_stage_and_mark_bot_stages(self):
        with tempfile.TemporaryDirectory() as tmp:
            container = runtime.Container(tmp, {})
            with mock.patch.object(runtime.subprocess, 'run',
                                   side_effect=runtime.subprocess.TimeoutExpired('docker', 1)):
                with self.assertRaises(runtime.ExecutionError) as caught:
                    container.shell('x', 1, stage='저장소 Playwright 브라우저 설치' + runtime.BOT_STAGE)
        self.assertEqual(str(caught.exception),
                         '시간 초과: 001.log — 저장소 Playwright 브라우저 설치 (봇 관리 단계 — 실행 계획과 무관) (1s)')

    def test_install_script_retries_a_stalled_download_and_moves_on(self):
        """Runs where it really runs: bash and coreutils `timeout` of the runtime image."""
        image = runtime.DEFAULT_IMAGE
        try:
            present = runtime.subprocess.run(['docker', 'image', 'inspect', image], capture_output=True,
                                             timeout=30).returncode == 0
        except (OSError, runtime.subprocess.SubprocessError):
            present = False
        if not present:
            self.skipTest('runtime image is not available locally')
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp, 'app')
            (project / 'node_modules/.bin').mkdir(parents=True)
            (project / 'package.json').write_text('{"devDependencies": {"@playwright/test": "1.61.0"}}')
            fake = project / 'node_modules/.bin/playwright'
            # The first install stalls past the per-attempt limit; the second succeeds.
            fake.write_text('#!/bin/bash\n[ "$1" = --version ] && { echo 1.61.0; exit 0; }\n'
                            'n=$(cat /work/count 2>/dev/null || echo 0); echo $((n+1)) > /work/count\n'
                            '[ "$n" = 0 ] && sleep 30\nexit 0\n')
            fake.chmod(0o755)
            proc = runtime.subprocess.run(
                ['docker', 'run', '--rm', '-v', tmp + ':/work', '-e', 'REVIEW_BROWSER_ATTEMPTS=3',
                 '-e', 'REVIEW_BROWSER_TIMEOUT=2', '-e', 'REVIEW_BROWSER_RETRY_DELAY=1', image,
                 'bash', '-e', '-o', 'pipefail', '-c', runtime.PLAYWRIGHT_BROWSERS],
                capture_output=True, text=True, timeout=120)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn('attempt 1/3', proc.stdout)
            self.assertNotIn('attempt 2/3', proc.stdout)
            self.assertEqual(Path(tmp, 'count').read_text().strip(), '2')
            # Every attempt failing reports failure instead of hanging.
            fake.write_text('#!/bin/bash\n[ "$1" = --version ] && { echo 1.61.0; exit 0; }\nsleep 30\n')
            proc = runtime.subprocess.run(
                ['docker', 'run', '--rm', '-v', tmp + ':/work', '-e', 'REVIEW_BROWSER_ATTEMPTS=2',
                 '-e', 'REVIEW_BROWSER_TIMEOUT=1', '-e', 'REVIEW_BROWSER_RETRY_DELAY=0', image,
                 'bash', '-e', '-o', 'pipefail', '-c', runtime.PLAYWRIGHT_BROWSERS],
                capture_output=True, text=True, timeout=120)
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            self.assertIn('attempt 2/2', proc.stdout)


def runpy_git(cwd, *args):
    return runtime.subprocess.run(['git', '-C', cwd] + list(args), check=True,
                                  capture_output=True, text=True).stdout


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

    def repair(self, answers, rerun=({'status': 'passed'}, ['/tmp/03.png'])):
        plan = {'scenarios': ['refresh re-enables HTML'], 'browser': 'old', 'start': 'vite'}
        evidence = {'cost_usd': 0, 'screenshots': ['/tmp/01.png', '/tmp/02.png'],
                    'browser': {'status': 'failed', 'output': 'AssertionError', 'failureKind': 'plan',
                                'triage': {'cause': 'script', 'summary': 'asserted mid-refresh',
                                           'screenEvidence': 'refresh still loading', 'repair': 'wait until loaded'}}}
        container = mock.Mock()
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(reviewer, '_run', side_effect=answers) as run, \
                mock.patch.object(runtime, '_run_browser', return_value=rerun) as browser:
            runtime._repair_browser(container, META, plan, evidence, Path(tmp), '/tmp', {}, lambda *_: None)
        return plan, evidence, container, run, browser

    def test_a_script_mistake_on_the_last_attempt_is_fixed_and_rerun_on_the_same_server(self):
        plan, evidence, container, run, browser = self.repair([
            {'browser': 'fixed', 'changes': 'waited for the refresh'},
            {'passed': True, 'complete': True, 'inspected': ['/tmp/03.png']}])
        self.assertIn('wait until loaded', run.call_args_list[0].args[0])
        self.assertIn('refresh re-enables HTML', run.call_args_list[0].args[0])
        self.assertEqual(plan['browser'], 'fixed')
        rerun_plan = browser.call_args.args[1]
        self.assertEqual((rerun_plan['browser'], rerun_plan['start']), ('fixed', ''))  # Server is still up.
        self.assertEqual(plan['start'], 'vite')
        container.shell.assert_called_once()
        self.assertIn('rm -f /evidence/*.png', container.shell.call_args.args[0])
        self.assertEqual(evidence['visual']['status'], 'passed')
        self.assertEqual(evidence['browserRepair']['status'], 'passed')
        self.assertEqual(evidence['browserRepair']['previous']['triage']['repair'], 'wait until loaded')

    def test_no_usable_repair_leaves_the_failure_as_it_was(self):
        for answer in ({'browser': ''}, {'browser': 'x', '_meta': {'salvaged': True}}):
            with self.subTest(answer=answer):
                plan, evidence, container, _, browser = self.repair([answer])
                browser.assert_not_called()
                container.shell.assert_not_called()
                self.assertEqual(plan['browser'], 'old')
                self.assertEqual(evidence['browserRepair']['status'], 'skipped')
                self.assertEqual(evidence['browser']['status'], 'failed')


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

    def test_scripts_get_a_context_and_a_settling_wait(self):
        script = runtime._browser_script('await until(() => true, "ready")')
        # #8793: context().newPage() failed on a page made by browser.newPage().
        self.assertIn('const context = await browser.newContext(', script)
        self.assertIn('const page = await context.newPage();', script)
        # A timed-out wait is an assertion failure, so it is triaged and softened on base like one.
        self.assertIn('new strict.AssertionError', script)
        self.assertLess(script.index('const until'), script.index('await until(() => true'))
        self.assertIn('until(check, message', runtime.Path(reviewer.ROOT, 'prompts/runtime.md').read_text())

    def test_base_run_keeps_going_past_assertions_about_what_the_pr_added(self):
        self.assertIn('const SOFT = true', runtime._browser_script('x', soft=True))
        self.assertIn('const SOFT = false', runtime._browser_script('x'))
        with tempfile.TemporaryDirectory() as tmp:
            container = runtime.Container(tmp, {})
            with mock.patch.object(container, 'shell', return_value={'status': 'passed', 'exitCode': 0, 'output': ''}), \
                    mock.patch.object(container, 'copy'), mock.patch.object(runtime, '_screenshots', return_value=[]):
                runtime._run_browser(container, {'start': '', 'browser': 'x'}, tmp, tmp, {}, soft=True)
            self.assertIn('const SOFT = true', (Path(tmp) / 'browser.cjs').read_text())

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

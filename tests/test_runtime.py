"""Execution evidence must reach reviewers and must not turn missing tests green."""
import copy
import json
import os
import time
from pathlib import Path
import runpy
import subprocess
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
    def test_junit_work_path_is_normalized_but_paths_outside_work_are_rejected(self):
        for supplied, expected in [('/work/results/test.xml', 'results/test.xml'),
                                   ('/tmp/test.xml', None), ('/work/../test.xml', None)]:
            plan = copy.deepcopy(PLAN)
            plan['tests'][0]['report'] = supplied
            with self.subTest(path=supplied), mock.patch.object(reviewer, '_run', return_value=plan), \
                    mock.patch.object(runtime, 'repository_context', return_value={}):
                if expected:
                    parsed = runtime._plan(META, [], '/tmp', {})
                    self.assertEqual(parsed['tests'][0]['report'], expected)
                else:
                    with self.assertRaises(runtime.ExecutionError):
                        runtime._plan(META, [], '/tmp', {})

    def test_case_scope_needs_nonempty_ids_and_reason(self):
        for cases, reason, valid in [(['c::aws'], 'AWS changed; SKPC unrelated', True),
                                     (['c::aws'], None, False), ([], 'scope', False),
                                     ([' '], 'scope', False)]:
            plan = copy.deepcopy(PLAN)
            plan['tests'][0].update(requiredCases=cases, report='results/test.xml')
            if reason is not None:
                plan['tests'][0]['scopeReason'] = reason
            with self.subTest(cases=cases, reason=reason), \
                    mock.patch.object(reviewer, '_run', return_value=plan), \
                    mock.patch.object(runtime, 'repository_context', return_value={}):
                if valid:
                    self.assertEqual(runtime._plan(META, [], '/tmp', {})['tests'][0]['requiredCases'], cases)
                else:
                    with self.assertRaises(runtime.ExecutionError):
                        runtime._plan(META, [], '/tmp', {})

    def test_planner_inventory_names_existing_mock_server_and_project_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, content in {
                'web/package.json': '{"packageManager":"pnpm@10.0.0"}',
                'web/mock-e2e/mockApi.mjs': '// mock server',
                'web/src/main.tsx': '// application entry',
                'web/src/Picker.tsx': '// changed component',
                'web/src/.env': 'not part of runtime context',
            }.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            subprocess.run(['git', 'init', '-q', tmp], check=True)
            subprocess.run(['git', '-C', tmp, 'add', '.'], check=True)
            context = runtime.repository_context([{'filename': 'web/src/Picker.tsx'}], tmp)
        self.assertIn('web/mock-e2e/mockApi.mjs', context['browserAssets'])
        self.assertIn('web/src/main.tsx', context['browserAssets'])
        self.assertIn('pnpm@10.0.0', context['projectFiles']['web/package.json'])
        self.assertNotIn('not part of runtime context', json.dumps(context))

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


class BaselineTests(unittest.TestCase):
    """head 에서 실패한 명령을 base 에서 다시 돌려, 그 실패가 PR 탓인지 가린다."""

    def test_baseline_failures_do_not_block_but_need_a_passing_test(self):
        both = dict(GOOD, tests=[{'status': 'passed'}, {'status': 'baseline', 'required': False}])
        self.assertEqual(runtime.approval_block(both), '')
        only_baseline = dict(GOOD, tests=[{'status': 'baseline', 'required': False}])
        self.assertIn('통과 근거', runtime.approval_block(only_baseline))
        regression = dict(GOOD, tests=[{'status': 'passed'}, {'status': 'failed', 'baseline': {'exitCode': 0}}])
        self.assertIn('실패', runtime.approval_block(regression))

    def _collect(self, base_fails, merge_base='base0'):
        plan = dict(copy.deepcopy(PLAN), visualApplicable=False,
                    tests=[{'name': 'unit', 'command': 'npm test'},
                           {'name': 'broad', 'command': 'npm run all'}])
        seen = []
        def command(args, *a, **kw):
            seen.append(args)
            if args[0] == 'git':
                Path(next(arg[9:] for arg in args if arg.startswith('--output='))).touch()
            return {'status': 'passed', 'exitCode': 0, 'output': ''}
        def shell(script, *a, **kw):
            runs = sum(1 for s in seen if s == ('shell', script))
            seen.append(('shell', script))
            if script == 'npm run all':
                fails = True if runs == 0 else base_fails       # 1회차 head, 2회차 기준선
                return {'status': 'failed' if fails else 'passed', 'exitCode': 1 if fails else 0, 'log': 'x'}
            return {'status': 'passed', 'exitCode': 0, 'log': 'x'}
        meta = dict(META, baseRefOid='basetip')
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(reviewer, 'STATE_DIR', tmp), \
                mock.patch.object(runtime, '_plan', return_value=plan), \
                mock.patch.object(runtime, '_merge_base', **({'side_effect': merge_base} if isinstance(merge_base, Exception) else {'return_value': merge_base})) as mb, \
                mock.patch.object(runtime.Container, 'command', side_effect=command), \
                mock.patch.object(runtime.Container, 'shell', side_effect=shell):
            result = runtime.collect(meta, [], tmp, {}, env={'GH_TOKEN': 't'})
        runtime._LIVE_CONTAINERS.clear()
        return result, seen, mb

    def test_command_failure_on_both_revisions_is_not_enough_to_exempt_it(self):
        result, seen, mb = self._collect(base_fails=True)
        broad = next(t for t in result['tests'] if t['name'] == 'broad')
        self.assertEqual(broad['status'], 'failed')
        self.assertEqual(broad['baseline']['exitCode'], 1)
        self.assertEqual(result['baseline']['status'], 'complete')
        self.assertEqual(result['baseline']['sha'], 'base0')
        self.assertTrue(runtime.approval_block(result))
        # 기준선 컨테이너는 base 소스로 만들고, 실패한 명령만 다시 돌린다
        archives = [c for c in seen if c[0] == 'git' and 'archive' in c]
        self.assertEqual([c[-1] for c in archives], ['abc', 'base0'])
        self.assertEqual([s for s in seen if s == ('shell', 'npm run all')].__len__(), 2)
        self.assertEqual([s for s in seen if s == ('shell', 'npm test')].__len__(), 1)
        self.assertEqual(sum(1 for c in seen if c[:2] == ['docker', 'rm']), 2)
        self.assertEqual(mb.call_args.args[1:3], ('basetip', 'abc'))

    def test_failure_only_on_head_is_a_regression(self):
        result, seen, _ = self._collect(base_fails=False)
        broad = next(t for t in result['tests'] if t['name'] == 'broad')
        self.assertEqual(broad['status'], 'failed')
        self.assertEqual(broad['baseline']['exitCode'], 0)
        self.assertIn('실패', runtime.approval_block(result))

    def test_unknown_baseline_keeps_the_failure(self):
        """기준선을 못 잡으면 봐주지 않는다 — 증거 없는 면제는 회귀를 숨긴다."""
        result, seen, _ = self._collect(base_fails=True, merge_base=runtime.ExecutionError('no base'))
        broad = next(t for t in result['tests'] if t['name'] == 'broad')
        self.assertEqual(broad['status'], 'failed')
        self.assertEqual(result['baseline']['status'], 'error')
        self.assertTrue(any('기준선' in n for n in result['notes']))
        self.assertIn('실패', runtime.approval_block(result))
        self.assertEqual(result['status'], 'complete')     # 기준선 실패가 검증 전체를 깨지 않는다

    def test_summary_labels_baseline_for_the_reader(self):
        body = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'review-bot'))['_summary_body']
        evidence = dict(GOOD, tests=[
            {'name': 'unit', 'command': 'npm test', 'status': 'passed'},
            {'name': 'broad', 'command': 'npm run all', 'status': 'baseline', 'baseline': {'exitCode': 1}},
            {'name': 'new', 'command': 'npm run new', 'status': 'failed', 'baseline': {'exitCode': 0}}],
            baseline={'status': 'complete', 'sha': 'base0123456789', 'tests': [{}, {}]})
        text = body({'findings': [], 'runtime': evidence, 'summary': ''}, [], [], None)
        self.assertIn('이 PR 탓이 아님', text)
        self.assertIn('이 PR 이 깨뜨림', text)
        self.assertNotIn('base 에서도 실패하는 테스트', text)
        self.assertIn('base `base012345` 에서 실패한 명령 2개', text)

    def test_preexisting_failures_are_pointed_out_with_both_causes(self):
        body = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'review-bot'))['_summary_body']
        evidence = dict(GOOD, tests=[{'name': 'full', 'command': 'pytest', 'status': 'baseline', 'cases': [
            {'id': 'svc::test_dispatch', 'status': 'baseline', 'message': "KeyError: 'event_id'",
             'baselineDiffers': True,
             'baselineMessage': 'Traceback\nE   sqlalchemy.exc.OperationalError: cannot commit\nE   (Background)'},
            {'id': 'svc::test_same', 'status': 'baseline', 'message': 'boom'},
            {'id': 'svc::test_ok', 'status': 'passed'}]}])
        text = body({'findings': [], 'runtime': evidence, 'summary': ''}, [], [], None)
        section = text[text.index('### base 에서도 실패하는 테스트'):text.index('### 검증 범위')]
        self.assertIn("`svc::test_dispatch` (full)", section)
        self.assertIn("head: `KeyError: 'event_id'`", section)
        self.assertIn('base: `sqlalchemy.exc.OperationalError: cannot commit` — 실패 원인이 head 와 다름', section)
        self.assertIn('`svc::test_same`', section)
        self.assertEqual(section.count('base:'), 1)
        self.assertNotIn('test_ok', section)


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

    def test_ui_plan_cannot_skip_browser_even_with_backend_unavailable(self):
        files = [{'filename': 'web/src/Picker.tsx', 'patch': '@@ -1 +1 @@\n-old\n+new'},
                 {'filename': 'web/src/Picker.css', 'patch': '@@ -1 +1 @@\n-a\n+b'}]
        for applicable, browser in ((False, ''), (True, '')):
            plan = dict(copy.deepcopy(PLAN), visualApplicable=applicable, browser=browser,
                        limitations=['authentication/backend unavailable'])
            with self.subTest(applicable=applicable), \
                    mock.patch.object(runtime, '_plan', return_value=plan) as planner, \
                    mock.patch.object(runtime, 'Container') as container:
                result = runtime.collect(META, files, self.temp.name, {})
            self.assertEqual(result['status'], 'incomplete')
            self.assertEqual(result['visualRequiredBy'], ['web/src/Picker.tsx', 'web/src/Picker.css'])
            self.assertEqual(planner.call_count, 3)
            container.assert_not_called()
            self.assertTrue(runtime.approval_block(result))
            # A later status refresh cannot make the planner's false flag waive the requirement.
            forged = dict(result, status='complete', limitations=[], tests=GOOD['tests'])
            self.assertIn('화면', runtime.approval_block(forged))
            self.assertFalse(runtime.approval_block(dict(forged, screenshots=['screen.png'],
                                                       visual={'status': 'passed', 'complete': True})))

    def test_test_only_and_documentation_changes_do_not_require_browser(self):
        files = [{'filename': 'web/src/__tests__/Picker.test.tsx'},
                 {'filename': 'docs/example.html'}, {'filename': 'README.md'},
                 {'filename': 'web/Picker.stories.tsx'}]
        plan = dict(PLAN, testsApplicable=False, visualApplicable=False, tests=[])
        with mock.patch.object(runtime, '_plan', return_value=plan), \
                mock.patch.object(runtime, 'Container') as container:
            result = runtime.collect(META, files, self.temp.name, {})
        container.assert_not_called()
        self.assertEqual(runtime.approval_block(result), '')

    def test_browser_repair_preserves_previous_scenarios(self):
        previous = {'plan': dict(PLAN, scenarios=['select user', 'first Escape keeps modal'])}
        plan = dict(copy.deepcopy(PLAN), scenarios=['select user'])
        with mock.patch.object(runtime, '_plan', return_value=plan), \
                mock.patch.object(runtime.Container, 'command', side_effect=OSError('offline')):
            result = runtime._collect_once(META, [], self.temp.name, {}, previous=previous)
        self.assertEqual(result['plan']['scenarios'], ['select user', 'first Escape keeps modal'])
        runtime._LIVE_CONTAINERS.clear()

    def test_rejected_plan_goes_back_to_the_planner_before_any_container_run(self):
        spawn = {'id': 'graph', 'name': 'graph', 'command': 'cd /work && uv run --locked pytest tests/graph.py'}
        previous = {'plan': dict(PLAN, tests=[spawn]),
                    'tests': [dict(spawn, status='failed', failureKind='plan',
                                   output='error: Failed to spawn: `pytest`')]}
        fixed = dict(spawn, command='cd /work && uv run --locked --extra dev pytest tests/graph.py',
                     repairReason='pytest lives in the dev extra')
        broken = dict(copy.deepcopy(PLAN), tests=[spawn], services=['postgres'])
        cases = (('repeated command', [dict(PLAN, tests=[spawn]), dict(PLAN, tests=[fixed])], '그대로 다시'),
                 ('invalid plan', [broken, dict(PLAN, tests=[fixed])], 'databaseSetup'))
        for label, plans, reason in cases:
            with self.subTest(label), \
                    mock.patch.object(runtime, '_plan', side_effect=copy.deepcopy(plans)) as planner, \
                    mock.patch.object(runtime.Container, 'command', side_effect=OSError('offline')):
                result = runtime._collect_once(META, [], self.temp.name, {}, previous=previous)
            self.assertEqual(planner.call_count, 2)
            self.assertNotIn('_plan_feedback', planner.call_args_list[0].args[0])
            self.assertIn(reason, planner.call_args_list[1].args[0]['_plan_feedback'])
            self.assertEqual(result['plan']['tests'][0]['command'], fixed['command'])
            runtime._LIVE_CONTAINERS.clear()

    def test_a_second_repeat_still_runs_rather_than_losing_the_attempt(self):
        spawn = {'id': 'graph', 'name': 'graph', 'command': 'uv run pytest'}
        previous = {'plan': dict(PLAN, tests=[spawn]),
                    'tests': [dict(spawn, status='failed', failureKind='plan')]}
        with mock.patch.object(runtime, '_plan', side_effect=lambda *a, **k: dict(copy.deepcopy(PLAN), tests=[dict(spawn)])) as planner, \
                mock.patch.object(runtime.Container, 'command', side_effect=OSError('offline')):
            result = runtime._collect_once(META, [], self.temp.name, {}, previous=previous)
        self.assertEqual(planner.call_count, 2)
        self.assertEqual(result['plan']['tests'][0]['command'], 'uv run pytest')
        runtime._LIVE_CONTAINERS.clear()

    def test_uv_spawn_failure_names_the_extra_that_provides_the_tool(self):
        root = Path(self.temp.name)
        (root / 'pyproject.toml').write_text('[project]\nname = "mono"\n'
                                             '[project.optional-dependencies]\ndev = ["pytest>=8.0.0", "ruff"]\n'
                                             '[dependency-groups]\nlint = ["mypy==1.0"]\n')
        (root / 'services' / 'api').mkdir(parents=True)
        failed = {'status': 'failed', 'output': 'error: Failed to spawn: `pytest`\n'}
        for command in ('cd /work && uv run --locked pytest tests', 'cd /work/services/api && uv run pytest'):
            hint = runtime.uv_tool_hint(dict(failed, command=command), self.temp.name)
            self.assertIn('--extra dev', hint)
        hint = runtime.uv_tool_hint(dict(failed, command='uv run mypy', output='Failed to spawn: `mypy`'), self.temp.name)
        self.assertIn('--group lint', hint)
        self.assertIsNone(runtime.uv_tool_hint(dict(failed, command='uv run x', output='Failed to spawn: `black`'), self.temp.name))
        self.assertIsNone(runtime.uv_tool_hint(dict(failed, command='cd /work && npm test', output='1 failed'), self.temp.name))

    def test_script_mistake_is_repaired_in_place_before_spending_a_recovery_attempt(self):
        def command(args, *a, **kw):
            if args[0] == 'git':
                Path(next(arg[9:] for arg in args if arg.startswith('--output='))).touch()
            return {'status': 'passed', 'exitCode': 0, 'output': ''}
        def screen(container, meta, plan, evidence, *a, **kw):
            evidence['browser'] = {'status': 'failed', 'failureKind': 'plan',
                                   'triage': {'cause': 'script', 'screenEvidence': 's', 'repair': 'wait'}}
            evidence['screenshots'] = ['/tmp/01.png']
        def repair(container, meta, plan, evidence, *a, **kw):
            evidence['browser'] = {'status': 'passed'}
            evidence['visual'] = {'status': 'passed', 'complete': True}
        for enabled, attempts, repairs in ((True, 1, 1), (False, 3, 0)):
            with self.subTest(enabled=enabled), \
                    mock.patch.object(runtime, '_plan', return_value=copy.deepcopy(PLAN)) as planner, \
                    mock.patch.object(runtime.Container, 'command', side_effect=command), \
                    mock.patch.object(runtime.Container, 'shell', return_value={'status': 'passed', 'exitCode': 0}), \
                    mock.patch.object(runtime, '_screen', side_effect=screen), \
                    mock.patch.object(runtime, '_repair_browser', side_effect=repair) as repaired:
                result = runtime.collect(META, [], self.temp.name, {'browserRepair': enabled})
            self.assertEqual((planner.call_count, repaired.call_count), (attempts, repairs))
            self.assertEqual(result['browser']['status'], 'passed' if enabled else 'failed')

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

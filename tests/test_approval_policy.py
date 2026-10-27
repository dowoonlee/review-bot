"""Regression cases from the approval audit; no remote reviews or containers are run."""
import copy
from contextlib import ExitStack
import json
from pathlib import Path
import runpy
import tempfile
import time
import unittest
from unittest import mock

from lib import checkpoints, config, github, policy, preflight, reviewer, runtime, store, validation, worktree

BOT = Path(__file__).resolve().parents[1] / 'review-bot'
GOOD = {'status': 'complete', 'sha': 'head', 'testsApplicable': True,
        'visualApplicable': False, 'limitations': [], 'tests': [{'status': 'passed', 'name': 'unit', 'command': 'pytest'}]}
META = {'state': 'OPEN', 'headRefOid': 'head', 'baseRefOid': 'base', 'changedFiles': 1,
        'title': 'change', 'author': {'login': 'author'}, '_repo': 'o/r', '_number': 1}
FILE = {'filename': 'a.py', 'patch': '@@ -1 +1 @@\n-old\n+new', 'additions': 1, 'deletions': 1}


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.bot = runpy.run_path(str(BOT))
        self.cfg = {'allowedEvents': ['APPROVE', 'COMMENT', 'REQUEST_CHANGES']}

    def finding(self, action):
        return {'severity': 'suggestion', 'disposition': action,
                'dispositionReason': 'observed impact or optional improvement',
                'path': 'a.py', 'line': 1, 'title': 'finding', 'body': 'details'}

    def test_optional_p2_approves_without_telling_author_to_fix_before_merge(self):
        f = self.finding('optional')
        self.assertEqual(self.bot['_guard_event']('APPROVE', [f], {'runtime': GOOD}, self.cfg, 'none'), ('APPROVE', ''))
        body = self.bot['_summary_body']({}, [f], [], {'event': 'APPROVE'})
        self.assertIn('선택적 개선', body)
        self.assertNotIn('머지 전', body)
        self.assertIn('선택적 개선', self.bot['_comment_body'](f))

    def test_guarded_summary_leads_with_final_decision(self):
        body = self.bot['_summary_body']({}, [], [], {
            'event': 'COMMENT', 'reason': '승인을 막을 공백이 없습니다.',
            'guard': 'aws::render (skipped) — AWS 계약 검증 필요'})
        self.assertIn('최종 판정 — 💬 Comment', body)
        self.assertIn('자동 판정의 참고 의견 (최종 판정과 별개)', body)
        self.assertLess(body.index('aws::render'), body.index('승인을 막을 공백'))
        self.assertNotIn('> **판정** — 승인을', body)

    def test_confirmed_p2_can_request_changes_but_uncertain_cannot(self):
        for action, expected in [('required', 'REQUEST_CHANGES'), ('optional', 'COMMENT'), ('uncertain', 'COMMENT')]:
            self.assertEqual(self.bot['_guard_event']('REQUEST_CHANGES', [self.finding(action)], {}, self.cfg)[0], expected)
        for action in ('required', 'uncertain'):
            self.assertEqual(self.bot['_guard_event']('APPROVE', [self.finding(action)], {}, self.cfg)[0], 'COMMENT')

    def test_failed_role_requires_specific_successful_coverage(self):
        payload = {'errors': ['security: timeout'], 'coverage': {'complete': False},
                   '_meta': {'agents': {'code': {}, 'security': {'error': 'timeout'}}}}
        self.assertTrue(policy.coverage_block(payload))
        payload['coverageExceptions'] = {'security': {'coveredBy': ['code'], 'reason': 'code traced the changed authorization boundary'}}
        self.assertFalse(policy.coverage_block(payload))
        payload['coverageExceptions']['security']['coveredBy'] = ['security']
        self.assertTrue(policy.coverage_block(payload))
        payload['coverage']['unavailable'] = ['missing.diff']
        self.assertTrue(policy.coverage_block(payload))

    def test_judge_preserves_actions_and_coverage_explanation(self):
        out = {'event': 'APPROVE', 'findings': [{'id': 0, 'placement': 'inline', 'disposition': 'optional', 'why': 'no current failure'}]}
        with mock.patch.object(reviewer, '_run', return_value=out):
            result = reviewer.judge(META, {}, [self.finding('optional')], ['APPROVE'])
        self.assertEqual(result['dispositions'][0]['disposition'], 'optional')


class TestEvidenceTests(unittest.TestCase):
    def test_large_reports_keep_full_results_but_bound_model_input(self):
        evidence = dict(GOOD, tests=[{'status': 'baseline', 'required': True,
                                    'cases': [{'id': str(i), 'status': 'passed'} for i in range(2000)] +
                                             [{'id': 'core', 'status': 'baseline'}]}])
        compact = runtime._prompt_evidence(evidence)
        test = compact['tests'][0]
        self.assertEqual(test['caseCounts'], {'passed': 2000, 'baseline': 1})
        self.assertEqual(test['requiredMissingCount'], 1)
        self.assertEqual(len(test['cases']), 40)
        self.assertEqual(len(evidence['tests'][0]['cases']), 2001)

    def cases(self, message='network unavailable'):
        return runtime.parse_junit(('''<testsuite tests="2" failures="1"><testcase classname="contract" name="new_contract"/>
            <testcase classname="contract" name="budget"><failure type="ConnectionError" message="%s">trace</failure></testcase></testsuite>''' % message).encode())

    def test_partial_passes_survive_same_failure_on_base(self):
        head = {'status': 'failed', 'cases': self.cases(), 'requiredCases': ['contract::new_contract']}
        runtime.compare_cases(head, {'status': 'failed', 'cases': self.cases()})
        self.assertEqual([c['status'] for c in head['cases']], ['passed', 'baseline'])
        self.assertEqual(runtime.approval_block(dict(GOOD, tests=[head])), '')
        # The same report must not approve when the unexecuted budget is a required contract.
        head['requiredCases'].append('contract::budget')
        self.assertIn('핵심', runtime.approval_block(dict(GOOD, tests=[head])))

    def test_mixed_environment_suite_uses_predeclared_scope(self):
        test = {'name': 'overlay', 'status': 'baseline', 'required': True,
                'requiredCases': ['overlay::aws'],
                'scopeReason': 'AWS 전용 변경; SKPC와 Tilt는 영향 없음',
                'cases': [{'id': 'overlay::aws', 'status': 'passed'},
                          {'id': 'overlay::skpc', 'status': 'skipped'},
                          {'id': 'overlay::tilt', 'status': 'baseline'}]}
        self.assertFalse(runtime.approval_block(dict(GOOD, tests=[test])))
        # The same outcomes block when a shared change also requires SKPC/Tilt.
        test['requiredCases'] += ['overlay::skpc', 'overlay::tilt']
        test['scopeReason'] = '공통 템플릿 변경으로 모든 환경에 영향'
        reason = runtime.approval_block(dict(GOOD, tests=[test]))
        for detail in ('overlay::skpc (skipped)', 'overlay::tilt (baseline)', '공통 템플릿'):
            self.assertIn(detail, reason)
        test['requiredCases'] = ['overlay::missing']
        self.assertIn('overlay::missing (보고서에 없음)', runtime.approval_block(dict(GOOD, tests=[test])))
        # A newly failing non-required case must not be silently exempted.
        test['requiredCases'] = ['overlay::aws']
        test['status'] = 'failed'
        test['cases'][1]['status'] = 'failed'
        self.assertTrue(runtime.approval_block(dict(GOOD, tests=[test])))

    def test_different_failure_on_base_never_exempts_head(self):
        head = {'status': 'failed', 'cases': self.cases('regression')}
        runtime.compare_cases(head, {'status': 'failed', 'cases': self.cases('old bug')})
        self.assertEqual(head['status'], 'failed')
        self.assertEqual(head['cases'][1]['status'], 'failed')

    def test_failure_signature_ignores_only_incidental_traceback_source_and_addresses(self):
        def report(address, code, message='DNS failure'):
            return runtime.parse_junit((
                '<testsuite><testcase classname="c" name="t"><failure type="ConnectionError" '
                'message="%s: object at %s">%s</failure></testcase></testsuite>'
                % (message, address, code)).encode())
        head = {'status': 'failed', 'cases': report('0xabc', 'new source')}
        runtime.compare_cases(head, {'cases': report('0x123', 'old source')})
        self.assertEqual(head['status'], 'baseline')

    def test_missing_report_is_not_baseline_and_skipped_required_case_blocks(self):
        head = {'status': 'failed'}
        runtime.compare_cases(head, {'status': 'failed'})
        self.assertEqual(head['status'], 'failed')
        head = {'status': 'passed', 'cases': [{'id': 'a', 'status': 'skipped'}]}
        self.assertTrue(runtime.approval_block(dict(GOOD, tests=[head])))

    def test_reports_reject_entities_and_unrepresented_collection_error(self):
        for xml in (b'<!DOCTYPE x><testsuite/>', b'<testsuite errors="1"><testcase name="a"/></testsuite>'):
            with self.assertRaises(ValueError):
                runtime.parse_junit(xml)

    def test_parameterized_duplicate_names_keep_results_without_false_baseline_matching(self):
        xml = b'<testsuite><testcase name="a"/><testcase name="a"><failure message="bad"/></testcase></testsuite>'
        cases = runtime.parse_junit(xml)
        self.assertEqual([case['status'] for case in cases], ['passed', 'failed'])
        self.assertEqual(len({case['id'] for case in cases}), 2)
        head = {'status': 'failed', 'cases': cases, 'requiredCases': ['::a']}
        runtime.compare_cases(head, {'cases': runtime.parse_junit(xml)})
        self.assertEqual(head['status'], 'failed')
        self.assertEqual(len(runtime.required_case_ids(head)), 2)
        self.assertTrue(runtime.approval_block(dict(GOOD, tests=[head])))

    def test_ci_replacement_requires_exact_head_check_and_scope(self):
        meta = dict(META, _ci_evidence={'sha': 'head', 'state': 'success', 'checks': [{'name': 'unit', 'conclusion': 'SUCCESS'}]})
        self.assertTrue(runtime.ci_covers(meta, ['unit'], 'workflow runs contract tests'))
        for names, reason in [(['other'], 'scope'), (['unit'], ''), ([], 'scope')]:
            self.assertFalse(runtime.ci_covers(meta, names, reason))
        meta['_ci_evidence']['sha'] = 'old'
        self.assertFalse(runtime.ci_covers(meta, ['unit'], 'scope'))

    def test_successful_ci_completes_missing_setup_but_does_not_erase_local_failure(self):
        meta = dict(META, _ci_evidence={'sha': 'head', 'state': 'success', 'checks': [{'name': 'unit', 'conclusion': 'SUCCESS'}]})
        spec = {'name': 'unit', 'command': 'pytest', 'ciChecks': ['unit'], 'ciReason': 'workflow runs exact changed test'}
        evidence = dict(GOOD, status='incomplete', tests=[], limitations=['setup failed'], plan={'tests': [spec], 'limitations': []})
        updated = runtime.refresh_ci(evidence, meta)
        self.assertEqual(runtime.approval_block(updated), '')
        evidence['tests'] = [dict(spec, status='failed')]
        self.assertTrue(runtime.approval_block(runtime.refresh_ci(evidence, meta)))

    def test_ci_only_plan_never_starts_duplicate_container(self):
        plan = {'testsApplicable': True, 'visualApplicable': False, 'setup': 'setup',
                'tests': [{'name': 'unit', 'command': 'pytest', 'ciChecks': ['unit'], 'ciReason': 'exact coverage'}],
                'start': '', 'browser': '', 'scenarios': [], 'limitations': []}
        meta = dict(META, _ci_evidence={'sha': 'head', 'state': 'success', 'checks': [{'name': 'unit', 'conclusion': 'SUCCESS'}]})
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(reviewer, 'STATE_DIR', tmp), \
                mock.patch.object(runtime, '_plan', return_value=plan), mock.patch.object(runtime, 'Container') as container:
            result = runtime.collect(meta, [], tmp, {})
        container.assert_not_called()
        self.assertFalse(runtime.approval_block(result))

    def test_runtime_recovery_runs_only_the_missing_command(self):
        plan = {'testsApplicable': True, 'visualApplicable': False, 'setup': '',
                'tests': [{'name': 'done', 'command': 'passed'}, {'name': 'missing', 'command': 'missing'}],
                'start': '', 'browser': '', 'scenarios': [], 'limitations': []}
        previous = dict(GOOD, tests=[{'name': 'done', 'command': 'passed', 'status': 'passed'}])
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(reviewer, 'STATE_DIR', tmp), \
                mock.patch.object(runtime, '_plan', return_value=plan), \
                mock.patch.object(runtime, '_provision', return_value={}), \
                mock.patch.object(runtime.Container, 'close'), \
                mock.patch.object(runtime, '_test', return_value={'name': 'missing', 'command': 'missing', 'status': 'passed'}) as test:
            result = runtime.collect(META, [], tmp, {}, previous=previous)
        runtime._LIVE_CONTAINERS.clear()
        self.assertEqual(test.call_count, 1)
        self.assertEqual(test.call_args.args[1]['command'], 'missing')
        self.assertTrue(result['tests'][0]['reused'])
        self.assertFalse(runtime.approval_block(result))

    def test_recovery_is_bounded_and_passes_successful_evidence_to_next_attempt(self):
        bad = dict(GOOD, status='incomplete', limitations=['setup failed'], cost_usd=1)
        with mock.patch.object(runtime, '_collect_once', side_effect=[bad, bad, GOOD]) as collect:
            result = runtime.collect(META, [], '/tmp', {})
        self.assertEqual(collect.call_count, 3)

        self.assertEqual(collect.call_args_list[1].args[-1]['tests'], GOOD['tests'])
        self.assertEqual(result['cost_usd'], 2)
        with mock.patch.object(runtime, '_collect_once', return_value=bad) as collect:
            runtime.collect(META, [], '/tmp', {})
        self.assertEqual(collect.call_count, 3)

    def test_browser_timeout_replans_with_failure_evidence_and_preserves_tests(self):
        for error in ('TimeoutError: missing button', 'strict mode violation: two buttons',
                      'TypeError: page.getByDisplayValue is not a function'):
            failed = dict(GOOD, browser={'status': 'failed', 'output': error}, screenshots=['failure.png'])
            with self.subTest(error=error), \
                    mock.patch.object(runtime, '_collect_once', side_effect=[failed, copy.deepcopy(GOOD)]) as collect:
                result = runtime.collect(META, [], '/tmp', {})
            self.assertEqual(collect.call_count, 2)
            self.assertEqual(collect.call_args.args[-1]['tests'], GOOD['tests'])
            self.assertIn('failure.png', collect.call_args.args[0]['_runtime_retry']['screenshots'])
            self.assertFalse(runtime.approval_block(result))

    def test_product_assertion_is_not_replanned_until_it_passes(self):
        failed = dict(GOOD, visualApplicable=True,
                      browser={'status': 'failed', 'output': 'AssertionError: footer overlaps list'})
        with mock.patch.object(runtime, '_collect_once', return_value=failed) as collect:
            result = runtime.collect(META, [], '/tmp', {})
        self.assertEqual(collect.call_count, 1)
        self.assertTrue(runtime.approval_block(result))


class RejudgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(store, 'STATE_DIR', self.temp.name))
        self.stack.enter_context(mock.patch.object(store, 'set_current'))
        self.stack.enter_context(mock.patch.object(store, 'clear_current'))
        self.stack.enter_context(mock.patch.object(preflight, 'check', return_value={'ok': True, 'checks': [], 'reason': ''}))
        self.bot = runpy.run_path(str(BOT))
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg['repos'] = {'o/r': {'enabled': True}}
        self.cfg['post'].update(dryRun=False, polish={'enabled': False})
        self.meta = dict(META, statusCheckRollup=[{'name': 'unit', 'conclusion': 'SUCCESS', 'status': 'COMPLETED'}])
        for name, value in [('active_account', 'me'), ('env_for', {}), ('pr_meta', self.meta),
                            ('last_review_request', None), ('review_requester', None), ('pr_files', [FILE])]:
            self.stack.enter_context(mock.patch.object(github, name, return_value=value))
        self.post = self.stack.enter_context(mock.patch.object(github, 'post_review'))
        self.stack.enter_context(mock.patch.dict(self.bot['process_pr'].__globals__, {'_log': lambda *_: None, '_verify_posted': lambda *_: None}))
        self.review = self.stack.enter_context(mock.patch.object(reviewer, 'review', side_effect=AssertionError('must reuse review')))
        self.prepare = self.stack.enter_context(mock.patch.object(worktree, 'prepare', side_effect=AssertionError('must reuse review')))
        self.judge = self.stack.enter_context(mock.patch.object(reviewer, 'judge', return_value={'event': 'APPROVE', 'reason': 'verified', 'placements': {}}))
        self.snapshot = {'identity': checkpoints.identity(self.meta, self.cfg, 'me'),
                         'payload': {'findings': [], 'summary': 'traced callers', 'errors': [], 'runtime': copy.deepcopy(GOOD)},
                         'files': [FILE], 'created': time.time(), 'waitingSince': time.time(),
                         'waiting': True, 'watchCI': True, 'ci': {'state': 'pending'}}
        checkpoints.save('o/r', 1, self.snapshot)

    def process(self):
        return self.bot['process_pr']({'repo': 'o/r', 'number': 1, '_rejudge': True}, self.cfg, {})

    def test_ci_completion_rejudges_without_repeating_review_and_posts_once(self):
        result = self.process()
        self.assertTrue(result['posted'])
        self.assertEqual(result['event'], 'APPROVE')
        self.review.assert_not_called()
        self.prepare.assert_not_called()
        self.assertEqual(self.post.call_count, 1)
        self.assertEqual(checkpoints.watching(self.cfg), [])
        self.assertIn('skipped', self.process())
        self.assertEqual(self.post.call_count, 1)

    def test_same_evidence_reuses_judgment_across_delivery_mode_change(self):
        self.process()
        self.assertEqual(self.judge.call_count, 1)
        self.cfg['post']['dryRun'] = True
        self.stack.enter_context(mock.patch.object(store, 'DRAFTS', self.temp.name))
        result = self.bot['process_pr']({'repo': 'o/r', 'number': 1}, self.cfg, {}, force=True, rejudge=True)
        self.assertEqual(result['cache'], {'review': True, 'runtime': True, 'decision': True})
        self.assertEqual(self.judge.call_count, 1)
        self.review.assert_not_called()
        self.prepare.assert_not_called()
        self.assertFalse(result['posted'])

    def test_changed_runtime_reuses_code_review_but_executes_and_rejudges(self):
        self.cfg['review']['runtime']['model'] = 'other'
        self.prepare.side_effect = None
        self.prepare.return_value = (self.temp.name, 'head', self.temp.name, 'ref')
        self.stack.enter_context(mock.patch.object(worktree, 'cleanup'))
        execution = self.stack.enter_context(mock.patch.object(runtime, 'collect', return_value=copy.deepcopy(GOOD)))
        result = self.bot['process_pr']({'repo': 'o/r', 'number': 1}, self.cfg, {}, force=True, rejudge=True)
        self.assertTrue(result['cache']['review'])
        self.assertFalse(result['cache']['runtime'])
        self.assertFalse(result['cache']['decision'])
        self.assertEqual(execution.call_count, 1)
        self.assertIsNone(execution.call_args.kwargs['previous'])
        self.review.assert_not_called()

    def test_changed_runtime_keeps_required_checks_but_not_their_outcomes(self):
        roundtrip = {'id': 'roundtrip', 'name': 'migration roundtrip', 'command': 'alembic downgrade && alembic upgrade'}
        self.snapshot['payload']['runtime'] = dict(
            copy.deepcopy(GOOD), plan={'tests': [roundtrip], 'scenarios': ['publish']}, requirements=[roundtrip],
            tests=[dict(roundtrip, status='failed', output='downgrade blocked', failureKind='test')])
        checkpoints.save('o/r', 1, self.snapshot)
        self.cfg['review']['runtime']['model'] = 'other'
        self.prepare.side_effect = None
        self.prepare.return_value = (self.temp.name, 'head', self.temp.name, 'ref')
        self.stack.enter_context(mock.patch.object(worktree, 'cleanup'))
        execution = self.stack.enter_context(mock.patch.object(runtime, 'collect', return_value=copy.deepcopy(GOOD)))
        self.bot['process_pr']({'repo': 'o/r', 'number': 1}, self.cfg, {}, force=True, rejudge=True)
        previous = execution.call_args.kwargs['previous']
        self.assertEqual([t['id'] for t in previous['requirements']], ['roundtrip'])
        self.assertEqual(previous['plan'], {'scenarios': ['publish']})
        self.assertNotIn('tests', previous)  # Old bot code's outcomes are not evidence.
        # A narrower new plan cannot drop the required check.
        plan = {'tests': [{'name': 'heads', 'command': 'alembic heads'}], 'scenarios': [], 'testsApplicable': True}
        planned, _ = validation.preserve(plan, previous)
        self.assertIn('alembic downgrade && alembic upgrade', [t['command'] for t in planned['tests']])

    def test_required_checks_survive_even_when_no_evidence_is_reusable(self):
        roundtrip = {'id': 'roundtrip', 'name': 'migration roundtrip', 'command': 'alembic downgrade && alembic upgrade'}
        self.snapshot['payload']['runtime'] = dict(copy.deepcopy(GOOD), requirements=[roundtrip])
        checkpoints.save('o/r', 1, self.snapshot)
        # A review-bot edit changes both keys, so load() gives nothing to reuse.
        changed = dict(self.snapshot['identity'], review='new', runtime='new')
        self.assertIsNone(checkpoints.load('o/r', 1, changed))
        self.assertEqual([t['id'] for t in checkpoints.obligations('o/r', 1, changed)['requirements']], ['roundtrip'])
        for field in ('head', 'base', 'account'):
            self.assertIsNone(checkpoints.obligations('o/r', 1, dict(changed, **{field: 'other'})))

    def test_unchanged_pending_ci_does_not_spend_on_judge_or_post(self):
        self.meta['statusCheckRollup'] = [{'name': 'unit', 'status': 'QUEUED'}]
        self.snapshot['ci'] = github.ci_evidence(self.meta)
        checkpoints.save('o/r', 1, self.snapshot)
        self.assertIn('skipped', self.process())
        self.judge.assert_not_called()
        self.post.assert_not_called()

    def test_changed_pending_ci_stays_internal_until_deadline(self):
        self.meta['statusCheckRollup'] = [{'name': 'unit', 'status': 'IN_PROGRESS'}]
        result = self.process()
        self.assertTrue(result['pending'])
        self.post.assert_not_called()
        saved = checkpoints.load('o/r', 1)
        saved['waitingSince'] = time.time() - 86401
        checkpoints.save('o/r', 1, saved)
        result = self.process()
        self.assertEqual(result['event'], 'COMMENT')
        self.assertEqual(self.post.call_count, 1)

    def test_changed_head_or_base_never_uses_old_approval(self):
        for field in ('headRefOid', 'baseRefOid'):
            old = self.meta[field]
            self.meta[field] = 'changed'
            self.assertIn('skipped', self.process())
            self.meta[field] = old
        self.post.assert_not_called()
        self.judge.assert_not_called()

    def test_closed_pr_stops_monitoring(self):
        self.meta['state'] = 'MERGED'
        self.assertIn('skipped', self.process())
        self.assertEqual(checkpoints.watching(self.cfg), [])
        self.post.assert_not_called()

    def test_remaining_required_finding_survives_green_ci(self):
        f = {'path': 'a.py', 'line': 1, 'severity': 'suggestion', 'title': 'bug', 'body': 'failure'}
        self.snapshot['payload']['findings'] = [f]
        checkpoints.save('o/r', 1, self.snapshot)
        self.judge.return_value = {'event': 'APPROVE', 'reason': 'green', 'placements': {},
                                  'dispositions': {0: {'disposition': 'required', 'dispositionReason': 'confirmed data loss'}}}
        result = self.process()
        self.assertEqual(result['event'], 'COMMENT')
        self.assertIn('수정', self.post.call_args.args[3])

    def test_fresh_review_waits_then_reuses_all_code_and_test_evidence(self):
        self.meta['statusCheckRollup'] = [{'name': 'unit', 'status': 'QUEUED'}]
        self.review.side_effect = None
        self.review.return_value = {'findings': [], 'summary': 'traced callers', 'errors': [], '_meta': {'cost_usd': 1}}
        self.prepare.side_effect = None
        self.prepare.return_value = (self.temp.name, 'head', self.temp.name, 'ref')
        for target, name, value in [(worktree, 'complete_patches', [FILE]),
                                    (worktree, 'file_history', {}),
                                    (github, 'file_review_comments', {})]:
            self.stack.enter_context(mock.patch.object(target, name, return_value=value))
        self.stack.enter_context(mock.patch.object(worktree, 'cleanup'))
        self.stack.enter_context(mock.patch.object(self.bot['janitor'], 'drop_session'))
        execution = self.stack.enter_context(mock.patch.object(runtime, 'collect', return_value=copy.deepcopy(GOOD)))
        result = self.bot['process_pr']({'repo': 'o/r', 'number': 1}, self.cfg, {}, force=True)
        self.assertTrue(result['pending'])
        self.post.assert_not_called()
        self.meta['statusCheckRollup'] = [{'name': 'unit', 'status': 'COMPLETED', 'conclusion': 'SUCCESS'}]
        self.assertEqual(self.process()['event'], 'APPROVE')
        self.assertEqual(self.review.call_count, 1)
        self.assertEqual(execution.call_count, 1)
        self.assertEqual(self.prepare.call_count, 1)

    def test_dry_run_rejudge_never_posts(self):
        self.cfg['post']['dryRun'] = True
        self.snapshot['identity'] = checkpoints.identity(self.meta, self.cfg, 'me')
        checkpoints.save('o/r', 1, self.snapshot)
        self.stack.enter_context(mock.patch.object(store, 'DRAFTS', self.temp.name))
        self.assertFalse(self.process()['posted'])
        self.post.assert_not_called()

    def test_exhausted_reviewer_errors_are_not_retried_on_every_ci_change(self):
        self.snapshot['payload'].update(errors=['security: failed'], _meta={'agents': {'security': {'error': 'failed'}}})
        checkpoints.save('o/r', 1, self.snapshot)
        result = self.process()
        self.assertEqual(result['event'], 'COMMENT')
        self.review.assert_not_called()
        self.prepare.assert_not_called()


class ReviewerRecoveryTests(unittest.TestCase):
    def test_only_failed_role_is_retried_and_success_is_reusable(self):
        calls = []
        def run(prompt, cwd, model, turns, timeout, tag, **kwargs):
            calls.append(tag)
            if tag.endswith('security') and calls.count(tag) == 1:
                raise reviewer.ReviewError('parse error', cost=1)
            return {'summary': 'traced', 'findings': [], '_meta': {'cost_usd': 2}}
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(reviewer, 'build_prompt', return_value='prompt'), \
                mock.patch.object(reviewer, '_run', side_effect=run):
            first = reviewer.review(META, [FILE], ['code', 'security'], tmp)
            self.assertEqual(calls.count('batch-001-code'), 1)
            self.assertEqual(calls.count('batch-001-security'), 2)
            self.assertEqual(first['_meta']['cost_usd'], 5)
            calls.clear()
            second = reviewer.review(META, [FILE], ['code', 'security'], tmp,
                                     previous_tasks=first['_tasks'])
            self.assertEqual(calls, [])
            self.assertEqual(second['_meta']['cost_usd'], 0)
            self.assertTrue(second['coverage']['complete'])


if __name__ == '__main__':
    unittest.main()

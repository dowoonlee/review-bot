from pathlib import Path
import runpy
import unittest

from lib import github, policy


class DecisionTests(unittest.TestCase):
    def test_guard_reason_explains_downgrade_and_preserves_original_judgment(self):
        result = policy.decision('APPROVE', 'COMMENT', [], {}, reason='looks good',
                                 guard='CI failed', ci='failure')
        self.assertEqual(result['summary'], 'CI failed')
        self.assertEqual(result['judgeEvent'], 'APPROVE')
        self.assertEqual(result['judgeReason'], 'looks good')
        self.assertEqual(result['finalEvent'], 'COMMENT')
        self.assertEqual(result['reasons'][0]['code'], 'CI_FAILED')

    def test_defect_human_check_and_environment_are_distinct(self):
        for action, code in [('required', 'DEFECT'), ('uncertain', 'HUMAN_REVIEW')]:
            report = policy.decision('COMMENT', 'COMMENT', [
                {'disposition': action, 'dispositionReason': 'evidence', 'title': 'finding'}], {})
            self.assertEqual(report['reasons'][0]['code'], code)
        report = policy.decision(None, 'PENDING', [], {}, environment={'ok': False, 'reason': 'Docker stopped'})
        self.assertEqual(report['reasons'][0]['code'], 'ENVIRONMENT_UNAVAILABLE')

    def test_late_ci_change_and_delivery_failure_keep_the_decision_chain(self):
        original = policy.decision('APPROVE', 'PENDING', [], {}, reason='code verified', ci='pending')
        self.assertIn('CI', original['summary'])
        updated = policy.revise_decision(original, 'COMMENT', 'PUBLISH_FAILED', 'API rejected review', 'github')
        self.assertEqual(updated['judgeEvent'], 'APPROVE')
        self.assertEqual(updated['finalEvent'], 'COMMENT')
        self.assertEqual(original['finalEvent'], 'PENDING')


class RevivedNitTests(unittest.TestCase):
    def finding(self, severity='nit', verdict='TRUE_POSITIVE', **verify):
        return {'title': 'nested ternary', 'severity': severity, '_verify': dict(verdict=verdict, **verify)}

    def test_a_verified_nit_dropped_as_taste_comes_back_as_an_optional_body_note(self):
        findings = [self.finding()]
        placements = {0: 'drop'}
        self.assertEqual(policy.revive_verified_nits(findings, placements, {}), [0])
        self.assertEqual(placements, {0: 'body'})
        self.assertEqual(policy.disposition(findings[0]), 'optional')
        # It is posted with an approval instead of blocking it.
        self.assertEqual(policy.finding_block('APPROVE', findings), '')

    def test_duplicates_scope_speculation_refuted_unverified_and_non_nits_stay_dropped(self):
        cases = [(self.finding(), {0: reason}) for reason in policy.DROP_REASONS]
        cases += [(self.finding(verdict='FALSE_POSITIVE'), {}), (self.finding(verdict=None), {}),
                  (self.finding(error='verify failed'), {}), (self.finding(severity='suggestion'), {}),
                  (self.finding(severity='blocking'), {})]
        for finding, reasons in cases:
            with self.subTest(finding=finding, reasons=reasons):
                placements = {0: 'drop'}
                self.assertEqual(policy.revive_verified_nits([finding], placements, reasons), [])
                self.assertEqual(placements, {0: 'drop'})

    def test_a_verified_design_alternative_survives_the_threshold_and_a_taste_drop(self):
        # #10151: minSeverity=suggestion cut a verified security design note before the
        # judge saw it, and the review said "지적 없음".
        bot = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'review-bot'))
        patch = '@@ -1,1 +1,2 @@\n ctx\n+new\n'
        files = {'a.py': {'postable': github.postable_lines(patch), 'added': github.added_lines(patch), 'patch': patch}}
        make = lambda severity, **verify: {'path': 'a.py', 'line': 2, 'severity': severity, 'title': severity + str(verify),
                                           'body': 'b', '_verify': verify}
        design = make('altitude', verdict='TRUE_POSITIVE', impact='concrete')
        findings = [design, make('nit', verdict='TRUE_POSITIVE'), make('altitude', verdict='TRUE_POSITIVE', error='x'),
                    make('altitude'), make('suggestion', verdict='TRUE_POSITIVE')]
        inline, demoted = bot['_validate'](findings, files, {'minSeverity': 'suggestion'})
        self.assertEqual([f['severity'] for f in inline + demoted], ['suggestion', 'altitude'])
        # A design alternative is never an inline comment, and never blocks an approval.
        kept, body, dropped = bot['_apply_placements'](inline + demoted, inline, {})
        self.assertEqual(([f['severity'] for f in kept], [f['severity'] for f in body]), (['suggestion'], ['altitude']))
        self.assertEqual(bot['_guard_event']('APPROVE', body, {}, {'allowedEvents': ['APPROVE', 'COMMENT']}), ('APPROVE', ''))
        text = bot['_summary_body']({}, [], body)
        self.assertIn('### 설계 대안 — 승인을 막지 않지만 검토를 권합니다', text)
        self.assertNotIn('지적 없음', text)
        self.assertNotIn('선택적 개선', text)
        # The judge dropping it as taste brings it back; a stated reason does not.
        placements = {0: 'drop'}
        self.assertEqual(policy.revive_verified_nits([design], placements, {}), [0])
        self.assertIn('설계 대안', design['dispositionReason'])
        self.assertEqual(policy.revive_verified_nits([dict(design)], {0: 'drop'}, {0: 'speculation'}), [])

    def test_findings_the_judge_kept_are_left_alone(self):
        placements = {0: 'inline'}
        self.assertEqual(policy.revive_verified_nits([self.finding()], placements, {}), [])
        self.assertEqual(placements, {0: 'inline'})

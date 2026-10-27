import unittest

from lib import policy


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

    def test_findings_the_judge_kept_are_left_alone(self):
        placements = {0: 'inline'}
        self.assertEqual(policy.revive_verified_nits([self.finding()], placements, {}), [])
        self.assertEqual(placements, {0: 'inline'})

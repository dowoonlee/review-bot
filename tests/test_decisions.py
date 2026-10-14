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

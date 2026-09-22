"""Maintenance observations must not displace actionable runtime/contract failures."""
import tempfile
import unittest
from unittest import mock
from lib import reviewer


class ImpactTests(unittest.TestCase):
    def test_impact_caps_only_maintenance_and_rejects_no_current_impact(self):
        finding = {'path': 'a.py', 'line': 1, 'severity': 'suggestion', 'title': 't', 'body': 'b'}
        for impact, expected, rejected in [('concrete', 'suggestion', False),
                                           ('maintainability', 'nit', False), ('none', None, True),
                                           (None, 'suggestion', False)]:
            with self.subTest(impact=impact), tempfile.TemporaryDirectory() as cwd, \
                    mock.patch.object(reviewer, '_run', return_value={
                        'verdict': 'TRUE_POSITIVE', 'severity': 'suggestion', 'impact': impact,
                        'reason': 'a.py:1 evidence', 'confidence': 99}):
                kept, dropped, _ = reviewer.verify([finding], {'_repo': 'o/r', '_number': 1}, cwd)
                self.assertEqual(bool(dropped), rejected)
                if kept:
                    self.assertEqual(kept[0]['severity'], expected)
                    if impact is None:
                        self.assertIn('error', kept[0]['_verify'])
                self.assertEqual(finding['severity'], 'suggestion')

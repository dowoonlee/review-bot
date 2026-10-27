import unittest

from lib import validation


class ValidationContractTests(unittest.TestCase):
    def previous(self, status='failed', kind='test'):
        test = {'id': 'contract', 'name': 'contract', 'command': 'pytest contract', 'required': True}
        return {'plan': {'tests': [test], 'scenarios': ['keep modal']},
                'tests': [dict(test, status=status, failureKind=kind)]}

    def test_dropped_or_optional_required_check_is_restored(self):
        for tests in ([], [{'id': 'contract', 'name': 'renamed', 'command': 'pytest contract', 'required': False,
                            'requiredCases': ['only-the-passing-case']}]):
            with self.subTest(tests=tests):
                plan, required = validation.preserve({'tests': tests, 'testsApplicable': False, 'scenarios': []}, self.previous())
                self.assertTrue(plan['testsApplicable'])
                self.assertTrue(plan['tests'][0]['required'])
                self.assertNotIn('requiredCases', plan['tests'][0])
                self.assertEqual(plan['scenarios'], ['keep modal'])
                self.assertEqual(required[0]['id'], 'contract')

    def test_product_failure_cannot_be_replaced_with_a_passing_command(self):
        plan, _ = validation.preserve({'tests': [{'id': 'contract', 'name': 'contract', 'command': 'true',
                                                'repairReason': 'skip failed test'}], 'scenarios': []}, self.previous())
        self.assertEqual(plan['tests'][0]['command'], 'pytest contract')

    def test_planner_failure_can_be_repaired_explicitly(self):
        repaired = {'id': 'contract', 'name': 'contract', 'command': 'pytest real_contract',
                    'repairReason': 'correct the nonexistent filename'}
        plan, _ = validation.preserve({'tests': [repaired], 'scenarios': []}, self.previous(kind='plan'))
        self.assertEqual(plan['tests'][0]['command'], 'pytest real_contract')
        repaired.pop('repairReason')
        # Unexplained: the obligation keeps its command and the rewrite runs beside it.
        plan, required = validation.preserve({'tests': [repaired], 'scenarios': []}, self.previous(kind='plan'))
        commands = {t['id']: t['command'] for t in plan['tests']}
        self.assertEqual(commands['contract'], 'pytest contract')
        self.assertIn('pytest real_contract', [c for i, c in commands.items() if i != 'contract'])
        self.assertEqual(len(commands), 2)
        self.assertIn('contract', [t['id'] for t in required])

    def test_rewrite_that_only_adds_environment_variables_is_not_run_twice(self):
        self.assertEqual(validation.command_core('export DB_SCHEMA_NAME=c2 && cd x && PYTHONPATH=.:app uv run pytest a.py'),
                         'cd x && uv run pytest a.py')
        rewritten = {'id': 'contract', 'name': 'contract', 'command': 'export DB_SCHEMA_NAME=c2 && pytest contract'}
        plan, _ = validation.preserve({'tests': [rewritten], 'scenarios': []}, self.previous(kind='plan'))
        self.assertEqual([t['command'] for t in plan['tests']], ['pytest contract'])
        different = dict(rewritten, command='export DB_SCHEMA_NAME=c2 && pytest other')
        plan, _ = validation.preserve({'tests': [different], 'scenarios': []}, self.previous(kind='plan'))
        self.assertEqual(len(plan['tests']), 2)

    def test_timeout_words_in_an_assertion_are_not_infrastructure_failure(self):
        self.assertEqual(validation.failure_kind({'status': 'failed', 'output': 'ConnectionError / TimeoutError',
                                                  'cases': [{'status': 'failed'}]}), 'test')

    def test_successful_command_survives_a_rewritten_plan(self):
        plan, _ = validation.preserve({'tests': [{'id': 'contract', 'name': 'contract', 'command': 'new command'}],
                                      'scenarios': []}, self.previous(status='passed'))
        self.assertEqual(plan['tests'][0]['command'], 'pytest contract')

    def test_observed_cases_cannot_disappear_from_an_environment_repair(self):
        previous = self.previous(kind='infrastructure')
        previous['tests'][0]['cases'] = [{'id': 'a', 'status': 'passed'}, {'id': 'b', 'status': 'failed'}]
        plan, _ = validation.preserve({'tests': [{'id': 'contract', 'name': 'contract', 'command': 'pytest only_a',
                                                'requiredCases': ['a'], 'repairReason': 'fix environment'}],
                                      'scenarios': []}, previous)
        self.assertEqual(set(plan['tests'][0]['requiredCases']), {'a', 'b'})
        self.assertFalse(validation.satisfied(dict(plan['tests'][0], status='passed', cases=[{'id': 'a', 'status': 'passed'}])))

    def test_skipped_required_case_is_not_a_completed_cache_entry(self):
        self.assertFalse(validation.satisfied({'status': 'passed', 'cases': [{'id': 'a', 'status': 'skipped'}]}))

    def test_scoped_obligations_survive_failure_and_retry(self):
        previous = self.previous()
        previous['plan']['tests'][0].update(requiredCases=['aws'], scopeReason='AWS changed; SKPC unrelated')
        previous['tests'][0]['cases'] = [{'id': 'aws', 'status': 'passed'}, {'id': 'skpc', 'status': 'failed'}]
        plan, requirements = validation.preserve({'tests': [], 'scenarios': []}, previous)
        self.assertEqual(requirements[0]['requiredCases'], ['aws'])
        self.assertEqual(plan['tests'][0]['scopeReason'], 'AWS changed; SKPC unrelated')
        previous['requirements'] = requirements
        plan, _ = validation.preserve({'tests': [{'id': 'contract', 'name': 'contract',
            'command': 'pytest contract', 'requiredCases': ['skpc'], 'scopeReason': 'changed rationale'}],
            'scenarios': []}, previous)
        self.assertEqual(set(plan['tests'][0]['requiredCases']), {'aws', 'skpc'})
        self.assertEqual(plan['tests'][0]['scopeReason'], 'AWS changed; SKPC unrelated')

"""A database requirement must provision an isolated DB, never use the host DB."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib import runtime, reviewer, validation


PLAN = {'testsApplicable': True, 'visualApplicable': False, 'setup': 'install deps',
        'databaseSetup': 'apply migrations', 'services': ['postgres'],
        'tests': [{'name': 'repo', 'command': 'pytest repo', 'requires': ['postgres']}],
        'start': '', 'browser': '', 'scenarios': [], 'limitations': []}


class PostgresTests(unittest.TestCase):
    def test_app_start_receives_the_same_database_environment_as_tests(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = runtime.Container(tmp, {})
            c.environment.update(runtime.POSTGRES_ENV)
            with mock.patch.object(c, 'command') as command:
                c.shell('start app', 30, detached=True)
            args = command.call_args.args[0]
            self.assertIn('-d', args)
            self.assertIn('DB_HOST=127.0.0.1', args)
            self.assertIn('DB_PORT=5432', args)
            self.assertEqual(args[-1], 'start app')

    def test_plan_rejects_unknown_services_and_missing_database_setup(self):
        for changes in ({'services': ['host-docker']}, {'databaseSetup': ''},
                        {'services': []}, {'services': 'postgres'}):
            with self.subTest(changes=changes), mock.patch.object(reviewer, '_run', return_value={**PLAN, **changes}), \
                    mock.patch.object(runtime, 'repository_context', return_value={}):
                with self.assertRaises(runtime.ExecutionError):
                    runtime._plan({'_ci_evidence': {}}, [], '/tmp', {})

    def test_db_provision_is_loopback_only_and_uses_owned_disposable_storage(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = runtime.Container(tmp, {})
            seen = []
            def command(args, *a, **kw):
                seen.append(args)
                if args[0] == 'git':
                    Path(next(arg[9:] for arg in args if arg.startswith('--output='))).touch()
                return {'status': 'passed', 'exitCode': 0, 'output': ''}
            with mock.patch.object(c, 'command', side_effect=command):
                result = runtime._provision(c, 'head', tmp, {}, PLAN, False)
                c.close()
            db = next(a for a in seen if a[:2] == ['docker', 'create'] and 'postgres:16-alpine' in a)
            self.assertIn('container:' + c.name, db)
            self.assertIn('--cap-drop=ALL', db)
            self.assertIn('--tmpfs', db)
            self.assertNotIn('-p', db)
            self.assertNotIn('/var/run/docker.sock', ' '.join(db))
            self.assertIn('review-bot.runtime=true', db)
            disconnect = next(i for i, a in enumerate(seen) if a[:3] == ['docker', 'network', 'disconnect'])
            self.assertLess(disconnect, seen.index(db))
            self.assertIn('databaseSetup', result)
            setup = next(a for a in seen if a[-1] == 'apply migrations')
            self.assertIn('DB_PORT=5432', setup)
            self.assertIn('DB_HOST=127.0.0.1', setup)
            removals = [a[-1] for a in seen if a[:2] == ['docker', 'rm']]
            self.assertEqual(removals, [c.name + '-postgres', c.name])
            self.assertNotIn(c.name + '-postgres', runtime._LIVE_CONTAINERS)

    def test_partial_db_creation_is_cleaned(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = runtime.Container(tmp, {})
            def command(args, *a, **kw):
                if args[:2] == ['docker', 'create']:
                    raise runtime.ExecutionError('create failed')
                return {'status': 'passed', 'exitCode': 0, 'output': ''}
            with mock.patch.object(c, 'command', side_effect=command) as calls:
                with self.assertRaises(runtime.ExecutionError):
                    c.start_postgres()
                c.close()
            self.assertTrue(any(call.args[0] == ['docker', 'rm', '-f', '-v', c.name + '-postgres']
                                for call in calls.call_args_list))

    def test_failed_service_cleanup_does_not_skip_runner_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = runtime.Container(tmp, {})
            c.services = [c.name + '-postgres']
            with mock.patch.object(c, 'command', side_effect=[OSError('failure'),
                                   {'exitCode': 0, 'output': ''}]) as calls:
                with self.assertRaises(runtime.ExecutionError):
                    c.close()
            self.assertEqual(calls.call_args_list[-1].args[0][-1], c.name)

    def test_previously_failed_test_is_rerun_after_database_environment_is_added(self):
        previous = {'plan': {**copy.deepcopy(PLAN), 'services': [], 'databaseSetup': ''},
                    'tests': [{'id': 'repo', 'name': 'repo', 'command': 'pytest repo',
                               'status': 'failed', 'cases': [{'id': 'repo::case', 'status': 'failed'}]}]}
        previous['plan']['tests'][0].update(id='repo', requires=[])
        plan = copy.deepcopy(PLAN)
        plan['tests'][0]['id'] = 'repo'
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(reviewer, 'STATE_DIR', tmp), \
                mock.patch.object(runtime, '_plan', return_value=plan), \
                mock.patch.object(runtime, '_provision', return_value={}), \
                mock.patch.object(runtime, '_baseline'), mock.patch.object(runtime.Container, 'close'), \
                mock.patch.object(runtime, '_test', return_value={'status': 'passed'}) as execute:
            result = runtime._collect_once({'_repo': 'o/r', '_number': 1, 'headRefOid': 'sha'}, [], tmp,
                                           {}, previous=previous)
        self.assertEqual(result['tests'][0]['status'], 'passed')
        self.assertEqual(execute.call_args.args[1]['command'], 'pytest repo')
        self.assertEqual(execute.call_args.args[1]['requiredCases'], ['repo::case'])
        runtime._LIVE_CONTAINERS.clear()

    def test_required_database_dependency_survives_repair(self):
        previous = {'plan': copy.deepcopy(PLAN)}
        plan = copy.deepcopy(PLAN)
        plan['tests'] = []
        restored, _ = validation.preserve(plan, previous)
        self.assertEqual(restored['tests'][0]['requires'], ['postgres'])

    def test_python_context_includes_fixture_and_marker_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = ['svc/pyproject.toml', 'svc/tests/conftest.py', 'svc/tests/test_repo.py']
            for name in files:
                path = Path(tmp, name)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('pytest.mark.db')
            with mock.patch.object(runtime.subprocess, 'run', return_value=mock.Mock(returncode=0, stdout='\0'.join(files))):
                context = runtime.repository_context([{'filename': files[-1]}], tmp)
            self.assertIn(files[0], context['projectFiles'])
            self.assertIn(files[1], context['testConfiguration'])

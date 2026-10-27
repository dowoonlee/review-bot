"""Required checks come from the head diff; a plan cannot omit them and approval needs them."""
import copy
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib import obligations, reviewer, runtime, validation


MIGRATION = ('"""add slug"""\nfrom typing import Sequence, Union\n\n'
             'revision: str = "0f75fc125e77"\n'
             'down_revision: Union[str, Sequence[str], None] = "8465dd7d2296"\n'
             'branch_labels = None\n')
MERGE = ('revision = \'feba12077d80\'\n'
         'down_revision = ("0f75fc125e77", "b3092a4a3c08")\n')
FILES = [{'filename': 'alembic/versions/0f75fc125e77_add_slug.py', 'status': 'added'},
         {'filename': 'alembic/versions/feba12077d80_merge.py', 'status': 'added'},
         {'filename': 'services/app/model.py', 'status': 'modified'}]
ROUNDTRIP = {'name': 'roundtrip', 'required': True, 'requires': ['postgres'], 'covers': ['migration-roundtrip'],
             'command': 'uv run alembic downgrade 8465dd7d2296 && uv run alembic upgrade head'}
PLAN = {'testsApplicable': True, 'visualApplicable': False, 'services': ['postgres'],
        'databaseSetup': 'uv run alembic upgrade head', 'setup': '', 'start': '', 'browser': '',
        'tests': [ROUNDTRIP], 'scenarios': [], 'limitations': []}
META = {'_repo': 'o/r', '_number': 1, 'headRefOid': 'head', '_ci_evidence': {}}


class DerivationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / 'alembic/versions').mkdir(parents=True)
        (root / 'alembic/versions/0f75fc125e77_add_slug.py').write_text(MIGRATION)
        (root / 'alembic/versions/feba12077d80_merge.py').write_text(MERGE)

    def test_changed_revisions_and_their_parents_are_read_from_the_worktree(self):
        checks = obligations.required_checks(FILES, self.temp.name)
        self.assertEqual([c['kind'] for c in checks], ['migration-roundtrip'])
        self.assertEqual(checks[0]['revisions'], ['0f75fc125e77', 'feba12077d80'])
        self.assertEqual(checks[0]['downRevisions'], ['0f75fc125e77', '8465dd7d2296', 'b3092a4a3c08'])
        self.assertEqual(checks[0]['paths'], [f['filename'] for f in FILES[:2]])

    def test_removed_revisions_and_non_version_paths_do_not_require_a_roundtrip(self):
        files = [{'filename': 'alembic/versions/0f75fc125e77_add_slug.py', 'status': 'removed'},
                 {'filename': 'alembic/env.py', 'status': 'modified'},
                 {'filename': 'docs/migrations/versions.md', 'status': 'modified'}]
        self.assertEqual(obligations.required_checks(files, self.temp.name), [])

    def test_ui_changes_keep_their_existing_rule(self):
        checks = obligations.required_checks([{'filename': 'web/src/Picker.tsx'}], self.temp.name)
        self.assertEqual(checks, [{'kind': 'visual', 'paths': ['web/src/Picker.tsx'], 'needs': checks[0]['needs']}])
        self.assertEqual(runtime.visual_required_paths([{'filename': 'web/src/Picker.tsx'}]), ['web/src/Picker.tsx'])


class ConfigContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        files = {'k8s/aws-prd/shared/configmap.yaml': 'FRONTEND_ORIGIN: x\n',
                 'services/frontend/nginx.conf': 'server {}\n',
                 'tests/test_origin.py': 'CONFIG = "k8s/aws-prd/shared/configmap.yaml"\n',
                 'tests/test_other_env.py': 'CONFIG = "k8s/aws/shared/configmap.yaml"\n',
                 'services/frontend/tests/test_headers.py': 'PATH = "services/frontend/nginx.conf"\n',
                 'services/frontend/src/a.test.ts': "read('services/frontend/nginx.conf')\n",
                 'docs/notes.md': 'k8s/aws-prd/shared/configmap.yaml\n'}
        for name, text in files.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(text)
        subprocess.run(['git', 'init', '-q', str(root)], check=True)
        subprocess.run(['git', '-C', str(root), 'add', '.'], check=True)
        self.changed = [{'filename': 'k8s/aws-prd/shared/configmap.yaml'},
                        {'filename': 'services/frontend/nginx.conf'},
                        {'filename': 'k8s/old.yaml', 'status': 'removed'}]

    def check(self):
        return next(c for c in obligations.required_checks(self.changed, self.temp.name) if c['kind'] == 'config-contract')

    def test_tests_that_name_a_changed_config_file_are_required(self):
        check = self.check()
        self.assertEqual(check['paths'], ['k8s/aws-prd/shared/configmap.yaml', 'services/frontend/nginx.conf'])
        # Exact path only: another environment's configmap and docs do not count.
        self.assertEqual(check['tests'], ['services/frontend/src/a.test.ts',
                                          'services/frontend/tests/test_headers.py', 'tests/test_origin.py'])

    def test_plan_must_run_every_referencing_test_file(self):
        check = self.check()
        def plan(*commands):
            return {'services': [], 'tests': [{'name': str(i), 'command': c, 'required': True, 'covers': ['config-contract']}
                                              for i, c in enumerate(commands)]}
        self.assertTrue(obligations.plan_gaps(plan('uv run pytest tests/test_origin.py'), [check]))
        self.assertEqual(obligations.plan_gaps(plan(
            'uv run pytest tests/test_origin.py --junitxml=/work/r.xml',
            'cd /work/services/frontend && PYTHONPATH=. uv run pytest tests && pnpm exec vitest run src/a.test.ts'), [check]), [])
        self.assertEqual(obligations.plan_gaps(plan('uv run pytest . services'), [check]), [])

    def test_config_change_without_referencing_tests_adds_no_obligation(self):
        changed = [{'filename': 'k8s/new-only.yaml'}]
        self.assertEqual([c['kind'] for c in obligations.required_checks(changed, self.temp.name)], [])

    def test_command_paths_follow_cd_and_ignore_flags_and_assignments(self):
        self.assertEqual(obligations.command_paths('cd /work/svc && X=1 uv run pytest tests/a.py::t -q --junitxml=/work/r.xml'),
                         {'svc/uv', 'svc/run', 'svc/pytest', 'svc/tests/a.py'})
        self.assertIn('.github/workflows/ci.yml', obligations.command_paths('pytest .github/workflows/ci.yml'))


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.checks = [{'kind': 'migration-roundtrip', 'paths': ['alembic/versions/a.py'],
                        'revisions': ['0f75fc125e77'], 'downRevisions': ['8465dd7d2296']}]

    def test_a_covering_postgres_roundtrip_satisfies_the_check(self):
        self.assertEqual(obligations.plan_gaps(PLAN, self.checks), [])

    def test_missing_cover_wrong_service_or_wrong_command_is_a_gap(self):
        for change in ({'covers': []}, {'required': False}, {'requires': []},
                       {'command': 'uv run alembic upgrade head'},
                       {'command': 'uv run alembic downgrade base && uv run alembic upgrade head'}):
            plan = copy.deepcopy(PLAN)
            plan['tests'][0].update(change)
            with self.subTest(change=change):
                self.assertTrue(obligations.plan_gaps(plan, self.checks))
        plan = copy.deepcopy(PLAN)
        plan['services'] = []
        plan['databaseSetup'] = ''
        self.assertTrue(obligations.plan_gaps(plan, self.checks))

    def test_planner_cannot_omit_a_derived_check(self):
        narrowed = copy.deepcopy(PLAN)
        narrowed['tests'] = [{'name': 'heads', 'command': 'uv run alembic heads', 'required': True}]
        with mock.patch.object(reviewer, '_run', return_value=narrowed), \
                mock.patch.object(obligations, 'required_checks', return_value=self.checks), \
                mock.patch.object(runtime, 'repository_context', return_value={}):
            with self.assertRaisesRegex(runtime.ExecutionError, 'migration-roundtrip'):
                runtime._plan(META, FILES, '/tmp', {})
        with mock.patch.object(reviewer, '_run', return_value=copy.deepcopy(PLAN)), \
                mock.patch.object(obligations, 'required_checks', return_value=self.checks), \
                mock.patch.object(runtime, 'repository_context', return_value={}):
            self.assertEqual(runtime._plan(META, FILES, '/tmp', {})['tests'][0]['covers'], ['migration-roundtrip'])

    def test_carried_requirements_are_shown_to_the_planner_on_the_first_attempt(self):
        carried = [{'id': 'test-f57c', 'name': 'roundtrip', 'command': ROUNDTRIP['command']}]
        with mock.patch.object(reviewer, '_run', return_value=copy.deepcopy(PLAN)) as run, \
                mock.patch.object(obligations, 'required_checks', return_value=[]), \
                mock.patch.object(runtime, 'repository_context', return_value={}):
            runtime._plan(META, [], '/tmp', {}, requirements=carried)
        self.assertIn('test-f57c', run.call_args.args[0])


class ExecutionTests(unittest.TestCase):
    def test_destructive_guard_is_lifted_only_for_the_roundtrip_on_the_disposable_db(self):
        db = dict(runtime.POSTGRES_ENV)
        self.assertEqual(obligations.test_environment(ROUNDTRIP, db), {'ALLOW_DESTRUCTIVE_MIGRATION': '1'})
        self.assertEqual(obligations.test_environment(ROUNDTRIP, {}), {})
        self.assertEqual(obligations.test_environment(dict(ROUNDTRIP, covers=[]), db), {})
        with tempfile.TemporaryDirectory() as tmp:
            container = runtime.Container(tmp, {})
            container.environment.update(db)
            with mock.patch.object(container, 'command', return_value={'status': 'passed', 'exitCode': 0, 'output': ''}) as command:
                result = runtime._run_test(container, ROUNDTRIP, {})
                self.assertIn('ALLOW_DESTRUCTIVE_MIGRATION=1', command.call_args.args[0])
                self.assertEqual(result['covers'], ['migration-roundtrip'])
                container.shell('start app', 30, detached=True)
                self.assertNotIn('ALLOW_DESTRUCTIVE_MIGRATION=1', command.call_args.args[0])

    def test_covers_survive_a_rewritten_plan(self):
        previous = {'plan': {'tests': [dict(ROUNDTRIP, id='rt')], 'scenarios': []},
                    'tests': [dict(ROUNDTRIP, id='rt', status='failed', failureKind='test')]}
        plan, _ = validation.preserve({'tests': [], 'scenarios': []}, previous)
        self.assertEqual(plan['tests'][0]['covers'], ['migration-roundtrip'])


class OptionalPlanErrorTests(unittest.TestCase):
    def test_an_optional_command_that_verified_nothing_does_not_block_approval(self):
        evidence = {'status': 'complete', 'limitations': [], 'testsApplicable': True, 'visualApplicable': False,
                    'tests': [{'name': 'unit', 'status': 'passed', 'command': 'pytest'},
                              {'name': 'grep', 'status': 'failed', 'required': False, 'failureKind': 'plan',
                               'command': 'playwright test --grep auto'}]}
        self.assertEqual(runtime.approval_block(evidence), '')
        for change in ({'failureKind': 'test'}, {'required': True}):
            with self.subTest(change=change):
                blocked = dict(evidence, tests=[evidence['tests'][0], dict(evidence['tests'][1], **change)])
                self.assertTrue(runtime.approval_block(blocked))


class ApprovalTests(unittest.TestCase):
    def evidence(self, **test):
        return {'status': 'complete', 'limitations': [], 'testsApplicable': True, 'visualApplicable': False,
                'requiredChecks': [{'kind': 'migration-roundtrip', 'paths': ['alembic/versions/a.py']}],
                'tests': [{**ROUNDTRIP, 'status': 'passed', **test},
                          {'name': 'unit', 'status': 'passed', 'command': 'pytest'}]}

    def test_derived_check_without_a_passing_covering_test_blocks_approval(self):
        self.assertEqual(runtime.approval_block(self.evidence()), '')
        for change in ({'status': 'failed'}, {'status': 'baseline'}, {'covers': []},
                       {'cases': [{'id': 'a', 'status': 'skipped'}]}):
            with self.subTest(change=change):
                self.assertTrue(runtime.approval_block(self.evidence(**change)))
        # The derived check is the backstop when nothing in the plan claims to cover it.
        self.assertIn('migration-roundtrip', runtime.approval_block(self.evidence(covers=[])))


if __name__ == '__main__':
    unittest.main()

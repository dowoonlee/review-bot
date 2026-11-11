"""The sandbox brings what CI brings, so a test fails on the code and not on a missing tool."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib import runtime, validation


WORKFLOW = '''jobs:
  contracts:
    steps:
      - name: Set up kubectl
        uses: azure/setup-kubectl@v4
        with:
          version: %s
      - run: pytest
'''
MAKE = "FileNotFoundError: [Errno 2] No such file or directory: 'make'"
STARLETTE = "ModuleNotFoundError: No module named 'starlette'"


def repository(tmp, files):
    def git(*args):
        subprocess.run(['git', '-C', tmp, '-c', 'user.name=t', '-c', 'user.email=t@t'] + list(args),
                       check=True, capture_output=True)
    git('init', '-q')
    for path, text in files.items():
        Path(tmp, path).parent.mkdir(parents=True, exist_ok=True)
        Path(tmp, path).write_text(text)
    git('add', '-A')
    git('commit', '-q', '-m', 'c')


def provision(tmp, plan=None):
    container = runtime.Container(tmp, {})
    seen = []

    def command(args, *a, **kw):
        seen.append(args)
        if args[0] == 'git':
            Path(next(arg[9:] for arg in args if arg.startswith('--output='))).touch()
        return {'status': 'passed', 'exitCode': 0, 'output': ''}
    with mock.patch.object(container, 'command', side_effect=command):
        evidence = runtime._provision(container, 'HEAD', tmp, {}, dict({'setup': 'pip install pytest', 'services': []},
                                                                     **(plan or {})), False)
    return evidence, seen


class RepositoryToolTests(unittest.TestCase):
    def test_make_and_the_kubectl_release_ci_pins_are_read_from_the_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository(tmp, {'Makefile': 'all:\n', '.github/workflows/ci.yml': WORKFLOW % 'v1.27.5',
                             '.github/workflows/dev.yaml': WORKFLOW % "'v1.27.5'",
                             '.github/workflows/old.yml': WORKFLOW % 'v1.30.1',
                             'docs/workflows/x.yml': WORKFLOW % 'v9.9.9'})
            self.assertEqual(runtime.repository_tools(tmp, 'HEAD'), {'make': True, 'kubectl': 'v1.27.5'})
        with tempfile.TemporaryDirectory() as tmp:
            repository(tmp, {'a.py': '', '.github/workflows/ci.yml': 'jobs: {}\n'})
            self.assertEqual(runtime.repository_tools(tmp, 'HEAD'), {})
            self.assertEqual(runtime.repository_tools(tmp, 'missing-sha'), {})

    def test_a_version_that_is_not_a_plain_release_is_never_passed_to_the_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository(tmp, {'.github/workflows/ci.yml': WORKFLOW % '$(curl evil)'})
            self.assertEqual(runtime.repository_tools(tmp, 'HEAD'), {})

    def test_tools_are_installed_before_the_plans_setup_while_the_network_is_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository(tmp, {'Makefile': 'all:\n', '.github/workflows/ci.yml': WORKFLOW % 'v1.27.5'})
            evidence, seen = provision(tmp)
        scripts = [a[-1] for a in seen]
        setup = scripts.index('pip install pytest')
        disconnect = next(i for i, a in enumerate(seen) if a[:3] == ['docker', 'network', 'disconnect'])
        self.assertLess(setup, disconnect)
        for script in (runtime.REPOSITORY_BOOTSTRAP, runtime.MAKE_BOOTSTRAP, runtime.KUBECTL_BOOTSTRAP):
            self.assertLess(scripts.index(script), setup)
        kubectl = seen[scripts.index(runtime.KUBECTL_BOOTSTRAP)]
        self.assertIn('REVIEW_KUBECTL_VERSION=v1.27.5', kubectl)
        self.assertEqual(evidence['tools'], {'make': True, 'kubectl': 'v1.27.5'})

    def test_a_repository_without_them_gets_a_git_tree_and_nothing_else(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository(tmp, {'a.py': ''})
            evidence, seen = provision(tmp)
        scripts = [a[-1] for a in seen]
        self.assertIn(runtime.REPOSITORY_BOOTSTRAP, scripts)
        self.assertNotIn(runtime.MAKE_BOOTSTRAP, scripts)
        self.assertNotIn(runtime.KUBECTL_BOOTSTRAP, scripts)
        self.assertNotIn('tools', evidence)


class MissingToolTests(unittest.TestCase):
    def case(self, message, status='failed', identity='t::a'):
        return {'id': identity, 'status': status, 'message': message, 'failure': message}

    def test_only_a_missing_executable_or_module_counts(self):
        for message, name in ((MAKE, 'make'), (STARLETTE, 'starlette'),
                              ('/bin/sh: 1: helm: command not found', 'helm'),
                              ("FileNotFoundError: [Errno 2] No such file or directory: 'docker'", None),
                              ("FileNotFoundError: [Errno 2] No such file or directory: '/work/k8s/a.yaml'", None),
                              ('AssertionError: key: not found', None)):
            with self.subTest(message=message):
                self.assertEqual(validation.missing_tool(self.case(message)), name)
        self.assertIsNone(validation.missing_tool(self.case(MAKE, status='baseline')))

    def test_a_tool_missing_on_base_too_is_not_a_base_failure(self):
        head = {'status': 'failed', 'cases': [self.case(MAKE), self.case('AssertionError: 10 == 9', identity='t::b'),
                                              {'id': 't::c', 'status': 'passed'}]}
        base = {'status': 'failed', 'cases': [self.case(MAKE), self.case('AssertionError: 10 == 9', identity='t::b')]}
        runtime.compare_cases(head, base)
        self.assertEqual([c['status'] for c in head['cases']], ['failed', 'baseline', 'passed'])
        self.assertEqual(head['status'], 'failed')
        # The bot's setup is what has to change, so the run is replanned rather than accepted.
        self.assertEqual(validation.failure_kind(head), 'plan')
        self.assertEqual(validation.missing_tools(head), ['make'])
        self.assertTrue(runtime.approval_block({'status': 'complete', 'limitations': [], 'testsApplicable': True,
                                                'visualApplicable': False, 'tests': [dict(head, failureKind='plan')]}))

    def test_the_replan_is_told_what_to_install_and_may_keep_the_command(self):
        failed = {'id': 'x', 'name': 'contracts', 'command': 'pytest tests', 'report': 'r.xml', 'status': 'failed',
                  'exitCode': 1, 'output': '', 'cases': [self.case(STARLETTE)]}
        evidence = {'status': 'complete', 'tests': [failed], 'limitations': [], 'cost_usd': 0,
                    'plan': {'setup': 'pip install pytest'}}
        with mock.patch.object(runtime, '_collect_once', return_value=evidence):
            result = runtime.collect({'_repo': 'o/r', '_number': 1}, [], None, {'recoveryAttempts': 0})
        self.assertIn('starlette', result['tests'][0]['diagnosis'])
        self.assertIn('review-apt-install', result['tests'][0]['diagnosis'])
        same = {'id': 'x', 'name': 'contracts', 'command': 'pytest tests', 'report': 'r.xml'}
        self.assertEqual(validation.repeated_plan_failures({'setup': 'pip install pytest', 'tests': [same]}, evidence), [same])
        self.assertEqual(validation.repeated_plan_failures(
            {'setup': 'pip install pytest starlette', 'tests': [same]}, evidence), [])


if __name__ == '__main__':
    unittest.main()

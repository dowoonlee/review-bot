"""Constructing a docker-py client is not a need for Docker; real use stays failed."""
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from lib import runtime, validation


SOCKET = ("docker.errors.DockerException: Error while fetching server API version: "
          "('Connection aborted.', FileNotFoundError(2, 'No such file or directory'))")
SHIMMED = "docker.errors.DockerException: review-bot: the validation sandbox provides no Docker daemon (...)"
SPEC = {'id': 'route', 'name': 'route', 'command': 'pytest tests/test_route.py', 'report': 'r.xml'}


def case(identity, status, message=None):
    row = {'id': identity, 'status': status}
    if message:
        row.update(message=message, failure='h-' + message[:20])
    return row


def outcome(*cases):
    status = 'failed' if any(c['status'] == 'failed' for c in cases) else 'passed'
    return {'status': status, 'exitCode': int(status == 'failed'), 'output': '', 'cases': list(cases)}


class DockerShimTests(unittest.TestCase):
    def run_test(self, *attempts):
        with tempfile.TemporaryDirectory() as tmp:
            container = runtime.Container(tmp, {})
            with mock.patch.object(runtime, '_run_test', side_effect=list(attempts)) as run, \
                    mock.patch.object(container, 'shell') as shell, \
                    mock.patch.object(container, 'copy') as copy:
                result = runtime._test(container, SPEC, {})
        return result, run, shell, copy

    def test_socket_failure_retries_once_with_the_shim(self):
        result, run, shell, copy = self.run_test(outcome(case('a', 'failed', SOCKET)), outcome(case('a', 'passed')))
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(result['dockerShim']['firstAttempt']['status'], 'failed')
        self.assertEqual(run.call_count, 2)
        self.assertEqual(copy.call_args.args, (runtime.DOCKER_SHIM, runtime.DOCKER_SHIM_DIR + '/review_bot_docker_shim.py'))
        scripts = [c.args[0] for c in shell.call_args_list]
        # Installed only around the retry, so later commands and the baseline run without it.
        self.assertEqual(scripts[-2:], [runtime.DOCKER_SHIM_INSTALL, runtime.DOCKER_SHIM_REMOVE])

    def test_shim_is_removed_even_when_the_retry_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            container = runtime.Container(tmp, {})
            with mock.patch.object(runtime, '_run_test', side_effect=[outcome(case('a', 'failed', SOCKET)), OSError('x')]), \
                    mock.patch.object(container, 'shell') as shell, mock.patch.object(container, 'copy'):
                with self.assertRaises(OSError):
                    runtime._test(container, SPEC, {})
        self.assertEqual(shell.call_args.args[0], runtime.DOCKER_SHIM_REMOVE)

    def test_real_docker_use_is_an_environment_failure_not_a_reusable_test_failure(self):
        result, *_ = self.run_test(outcome(case('a', 'failed', SOCKET)), outcome(case('a', 'failed', SHIMMED)))
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['failureKind'], 'environment')
        self.assertIn('관련 로컬 테스트가 실패', runtime.approval_block(
            {'status': 'complete', 'limitations': [], 'tests': [result]}))

    def test_assertion_failures_are_not_retried(self):
        result, run, *_ = self.run_test(outcome(case('a', 'failed', SOCKET), case('b', 'failed', 'AssertionError: 1 != 2')))
        self.assertEqual(run.call_count, 1)
        self.assertEqual(result['failureKind'], 'test')

    def test_a_docker_absent_skip_is_not_turned_into_a_failure(self):
        result, *_ = self.run_test(outcome(case('a', 'failed', SOCKET), case('guard', 'skipped')),
                                   outcome(case('a', 'passed'), case('guard', 'failed', SHIMMED)))
        self.assertEqual(result['status'], 'passed')
        self.assertEqual([c['status'] for c in result['cases']], ['passed', 'skipped'])

    def test_a_regression_revealed_by_the_retry_stays_a_test_failure(self):
        result, *_ = self.run_test(outcome(case('a', 'failed', SOCKET)),
                                   outcome(case('a', 'failed', 'AssertionError: 307 != 200')))
        self.assertEqual(result['failureKind'], 'test')

    def test_unparseable_report_is_not_attributed_to_docker(self):
        self.assertFalse(validation.docker_unavailable({'status': 'failed', 'output': SOCKET, 'reportError': 'x'}))
        self.assertTrue(validation.docker_unavailable({'status': 'failed', 'output': SOCKET}))


@unittest.skipUnless(importlib.util.find_spec('docker'), 'docker-py is not installed')
class DockerShimModuleTests(unittest.TestCase):
    def test_client_builds_without_a_daemon_but_api_calls_fail_with_the_marker(self):
        probe = ('import review_bot_docker_shim, docker\n'
                 'client = docker.from_env()\n'
                 'try:\n    client.ping()\n'
                 'except docker.errors.DockerException as exc:\n    print(exc)\n')
        env = dict(os.environ, DOCKER_HOST='unix:///nonexistent/review-bot.sock',
                   PYTHONPATH=str(runtime.DOCKER_SHIM.parent))
        out = subprocess.run([sys.executable, '-c', probe], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(validation.docker_unavailable_case({'message': 'DockerException: ' + out.stdout}))


if __name__ == '__main__':
    unittest.main()

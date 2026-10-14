import json
from pathlib import Path
import runpy
from types import SimpleNamespace
import unittest
from unittest import mock

from lib import preflight, config, github, reviewer, worktree, store


class PreflightTests(unittest.TestCase):
    def check(self, free=20, memory=8, returncode=0):
        with mock.patch.object(preflight.shutil, 'which', return_value='/bin/docker'), \
                mock.patch.object(preflight.shutil, 'disk_usage', return_value=SimpleNamespace(free=free * 1024**3)), \
                mock.patch.object(preflight.subprocess, 'run', return_value=SimpleNamespace(
                    returncode=returncode, stdout=json.dumps({'memory': memory * 1024**3, 'cpus': 8}))):
            return preflight.check({'memory': '6g', 'cpus': 4}, '/tmp')

    def test_healthy_and_unavailable_resources(self):
        self.assertTrue(self.check()['ok'])
        for kwargs, code in [({'free': 1}, 'DISK_SPACE'), ({'memory': 4}, 'DOCKER_MEMORY'),
                             ({'returncode': 1}, 'DOCKER_AVAILABLE')]:
            with self.subTest(kwargs=kwargs):
                report = self.check(**kwargs)
                self.assertFalse(report['ok'])
                self.assertTrue(any(c['code'] == code and not c['ok'] for c in report['checks']))

    def test_disabled_runtime_does_not_probe_docker(self):
        with mock.patch.object(preflight.subprocess, 'run') as run:
            self.assertTrue(preflight.check({'enabled': False}, '/tmp')['ok'])
        run.assert_not_called()

    def test_unavailable_environment_defers_without_models_worktree_or_posting(self):
        bot = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'review-bot'))
        cfg = config._merge(config.DEFAULTS, {'repos': {'o/r': {'enabled': True}}, 'post': {'dryRun': False}})
        with mock.patch.object(github, 'active_account', return_value='me'), \
                mock.patch.object(github, 'env_for', return_value={}), \
                mock.patch.object(github, 'pr_meta', return_value={'state': 'OPEN', 'headRefOid': 'sha'}), \
                mock.patch.object(github, 'last_review_request', return_value=None), \
                mock.patch.object(github, 'pr_files', return_value=[{'filename': 'a.py'}]), \
                mock.patch.object(preflight, 'check', return_value={'ok': False, 'reason': 'Docker stopped', 'checks': []}), \
                mock.patch.object(reviewer, 'review') as review, \
                mock.patch.object(worktree, 'prepare') as prepare, \
                mock.patch.object(github, 'post_review') as post, \
                mock.patch.object(store, 'clear_current'), \
                mock.patch.dict(bot['process_pr'].__globals__, {'_log': lambda *_: None}):
            result = bot['process_pr']({'repo': 'o/r', 'number': 1}, cfg, {})
        self.assertEqual(result['event'], 'PENDING')
        self.assertTrue(result['deferred'])
        for call in (review, prepare, post):
            call.assert_not_called()
        state = {}; store.mark(state, 'o/r', 1, 'sha', pending=True, posted=False)
        self.assertFalse(store.already_done(state, 'o/r', 1, 'sha', True))

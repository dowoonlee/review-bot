"""Exercise ownership and interruption recovery without touching production resources."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import runpy
import select
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from lib import janitor, resources, runtime, store, worktree

ROOT = Path(__file__).resolve().parents[1]


class LockTests(unittest.TestCase):
    def test_live_owner_is_exclusive_and_sigkill_releases_os_lock(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            for key, value in [('STATE_DIR', tmp), ('DRAFTS', str(Path(tmp, 'drafts'))), ('LOCK', str(Path(tmp, 'tick.lock')))]:
                stack.enter_context(mock.patch.object(store, key, value))
            script = '''from lib import store
import sys,time
from pathlib import Path
store.STATE_DIR=sys.argv[1];store.DRAFTS=str(Path(sys.argv[1],'drafts'));store.LOCK=str(Path(sys.argv[1],'tick.lock'))
owned=store.acquire_lock();print(owned,flush=True)
if sys.argv[2]=='wait': time.sleep(60)
store.release_lock()
'''
            try:
                self.assertTrue(store.acquire_lock())
                other = subprocess.run([sys.executable, '-c', script, tmp, 'exit'], cwd=ROOT,
                                       capture_output=True, text=True, timeout=10)
                self.assertEqual(other.stdout.strip(), 'False')
                self.assertTrue(Path(store.LOCK).exists())  # Non-owner release cannot remove our marker.
                store.release_lock()
                child = subprocess.Popen([sys.executable, '-c', script, tmp, 'wait'], cwd=ROOT,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    self.assertTrue(select.select([child.stdout], [], [], 10)[0])
                    self.assertEqual(child.stdout.readline().strip(), 'True')
                    self.assertFalse(store.acquire_lock())
                    child.kill();child.wait(timeout=10)
                    self.assertTrue(store.acquire_lock())
                finally:
                    if child.poll() is None: child.kill();child.wait(timeout=10)
                    child.stdout.close();child.stderr.close()
            finally:
                store.release_lock()

    def test_manual_gc_never_runs_when_lock_is_busy(self):
        bot = runpy.run_path(str(ROOT/'review-bot'))
        gc = mock.Mock()
        with mock.patch.object(store, 'acquire_lock', return_value=False), \
                mock.patch.dict(bot['cmd_gc'].__globals__, {'_gc': gc, '_log': lambda *_: None}):
            self.assertEqual(bot['cmd_gc'](None, {}), 1)
        gc.assert_not_called()

    def test_manual_gc_reports_failure_and_always_releases_lock(self):
        bot = runpy.run_path(str(ROOT/'review-bot'))
        with mock.patch.object(store, 'acquire_lock', return_value=True), \
                mock.patch.object(store, 'release_lock') as release, \
                mock.patch.dict(bot['cmd_gc'].__globals__, {'_gc': lambda *a, **kw: {'errors': ['failed']}}):
            self.assertEqual(bot['cmd_gc'](None, {}), 1)
        release.assert_called_once()


class ContainerRecoveryTests(unittest.TestCase):
    def sweep(self, fail=False):
        def run(args, **kwargs):
            if args[1] == 'ps': return SimpleNamespace(returncode=0, stdout='dead\nlive\nforeign\n', stderr='')
            if args[1] == 'inspect':
                name = args[2]
                labels = {'review-bot.instance': 'other' if name == 'foreign' else 'ours',
                          'review-bot.owner-pid': '2' if name == 'live' else '1', 'review-bot.owner-start': 'start'}
                return SimpleNamespace(returncode=0, stderr='', stdout=json.dumps([{'Name': 'review-bot-'+name, 'Config': {'Labels': labels}}]))
            return SimpleNamespace(returncode=1 if fail else 0, stdout='', stderr='denied' if fail else '')
        with mock.patch.object(resources, 'instance_id', return_value='ours'), \
                mock.patch.object(resources, 'alive', side_effect=lambda owner: owner['pid'] == '2'), \
                mock.patch.object(resources.subprocess, 'run', side_effect=run) as calls:
            result = resources.sweep_containers()
        removals = [c.args[0] for c in calls.call_args_list if c.args[0][1] == 'rm']
        return result, removals

    def test_only_our_dead_owner_is_removed_with_its_volumes(self):
        report, removals = self.sweep()
        self.assertEqual(removals, [['docker', 'rm', '-f', '-v', 'dead']])
        self.assertEqual(report['removed'], ['review-bot-dead'])
        self.assertEqual(report['kept'], ['review-bot-live'])

    def test_removal_failure_is_reported_and_blocks_dependent_cleanup(self):
        report, _ = self.sweep(fail=True)
        self.assertEqual(report['removed'], [])
        self.assertTrue(report['errors'])
        self.assertIn('1:start', report['blocked'])

    def test_pid_reuse_and_unknown_identity_are_distinguished(self):
        self.assertTrue(resources.alive({'pid': os.getpid()}))
        with mock.patch.object(resources, 'process_start', return_value='new-start'):
            self.assertFalse(resources.alive({'pid': os.getpid(), 'processStarted': 'old-start'}))
            self.assertTrue(resources.alive({'pid': os.getpid(), 'processStarted': 'new-start'}))


class WorktreeRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name, 'repo');self.base.mkdir()
        self.git('init', '-q');(self.base/'file.txt').write_text('fixture')
        self.git('add', 'file.txt')
        self.git('-c','user.name=Test','-c','user.email=test@example.invalid','-c','commit.gpgsign=false',
                 '-c','core.hooksPath=/dev/null','commit','-qm','fixture')
        self.git('remote','add','origin',str(self.base));self.git('update-ref','refs/pull/1/head','HEAD')
        patch = mock.patch.object(worktree, 'WT_DIR', str(Path(self.temp.name, 'wt')))
        patch.start();self.addCleanup(patch.stop)
        self.config = {'audit/repo': {'localPath': str(self.base)}}

    def git(self, *args):
        return subprocess.run(['git','-C',str(self.base),*args], capture_output=True, text=True, check=True).stdout.strip()

    def test_failed_acquisition_cleans_ref_and_reports_failed_cleanup_for_next_sweep(self):
        original = worktree._git
        def fail(repo, args, *a, **kw):
            if args[:2] in (['worktree','add'], ['update-ref','-d']):
                raise worktree.WorktreeError('simulated failure')
            return original(repo, args, *a, **kw)
        with mock.patch.object(worktree, '_git', side_effect=fail):
            with self.assertRaisesRegex(worktree.WorktreeError, '정리 미완료'):
                worktree.prepare('audit/repo', 1, str(self.base))
        self.assertTrue(self.git('for-each-ref','--format=%(refname)',worktree._ref_prefix()))
        with mock.patch.object(resources, 'alive', return_value=False):
            report = worktree.sweep(self.config)
        self.assertFalse(report['errors'])
        self.assertFalse(self.git('for-each-ref','--format=%(refname)',worktree._ref_prefix()))

    def test_live_worktree_is_preserved_and_ref_only_orphan_is_collected(self):
        fetch_head = self.base / '.git/FETCH_HEAD'
        fetch_head.write_text('user fetch state\n')
        path, _, base, ref = worktree.prepare('audit/repo', 1, str(self.base))
        self.assertEqual(fetch_head.read_text(), 'user fetch state\n')
        orphan = worktree._ref_prefix()+'pr-2'
        foreign = 'refs/review-bot/foreign/pr-3'
        self.git('update-ref',orphan,'HEAD');self.git('update-ref',foreign,'HEAD')
        report = worktree.sweep(self.config)
        self.assertTrue(Path(path).exists())
        self.assertTrue(self.git('for-each-ref','--format=%(refname)',ref))
        self.assertFalse(self.git('for-each-ref','--format=%(refname)',orphan))
        self.assertTrue(self.git('for-each-ref','--format=%(refname)',foreign))
        self.assertEqual(report['removed'], 1)
        self.assertEqual(worktree.cleanup(base, path, ref), [])


class EvidenceRecoveryTests(unittest.TestCase):
    def test_superseded_runs_are_removed_right_after_a_newer_review_replaces_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            pr = Path(tmp, 'evidence/o_r/1')
            saved = time.time()
            earlier = saved - 600
            runs = {name: pr / name for name in ('oldhead/a', 'head/kept', 'head/attempt', 'head/report',
                                                 'head/stale', 'head/live', 'head/foreign', 'head/after')}
            owners = {'live': {'instance': 'ours', 'pid': 2}, 'foreign': {'instance': 'other', 'pid': 1}}
            for name, path in runs.items():
                path.mkdir(parents=True)
                (path / 'result.json').write_text('{}')
                if path.name in owners:
                    (path / 'owner.json').write_text(json.dumps(owners[path.name]))
                if path.name != 'after':  # 'after' is a failed review that ran after the save.
                    for p in (*path.iterdir(), path):
                        os.utime(p, (earlier, earlier))
            other_pr = Path(tmp, 'evidence/o_r/2/head/x')
            other_pr.mkdir(parents=True)
            checkpoint = {'updated': saved, 'payload': {'runtime': {
                'directory': str(runs['head/kept']),
                'attempts': [{'directory': str(runs['head/attempt'])}],
                'tests': [{'reused': True, 'reportArtifact': str(runs['head/report'] / 'junit-1.xml')}]}}}
            with mock.patch.object(janitor, 'STATE_DIR', tmp), \
                    mock.patch.object(resources, 'instance_id', return_value='ours'), \
                    mock.patch.object(resources, 'alive', side_effect=lambda owner: owner['pid'] == 2):
                self.assertEqual(janitor.prune_superseded(checkpoint), 2)
                self.assertEqual(janitor.prune_superseded({'payload': {'runtime': {}}}), 0)
            gone = {'oldhead/a', 'head/stale'}
            for name, path in runs.items():
                self.assertEqual(path.exists(), name not in gone, name)
            self.assertFalse((pr / 'oldhead').exists())
            self.assertTrue(other_pr.exists())

    def test_old_interrupted_runs_are_reaped_but_live_and_foreign_owners_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp,'evidence/repo/1/head')
            old = time.time()-30*86400
            for name, owner in [('old', {'instance':'ours','pid':1}), ('live',{'instance':'ours','pid':2}),
                                ('foreign',{'instance':'other','pid':1}), ('legacy',None), ('recent',{'instance':'ours','pid':1})]:
                path=root/name;path.mkdir(parents=True)
                if owner: (path/'owner.json').write_text(json.dumps(owner))
                (path/'source.tar').write_bytes(b'archive')
                if name!='recent':
                    for p in path.iterdir(): os.utime(p,(old,old))
                    os.utime(path,(old,old))
            with mock.patch.object(janitor,'STATE_DIR',tmp), \
                    mock.patch.object(resources,'instance_id',return_value='ours'), \
                    mock.patch.object(resources,'alive',side_effect=lambda owner: owner['pid']==2):
                self.assertEqual(janitor.prune_evidence(14),2)
            self.assertFalse((root/'old').exists());self.assertFalse((root/'legacy').exists())
            self.assertTrue((root/'live/source.tar').exists());self.assertTrue((root/'foreign/source.tar').exists())
            self.assertTrue((root/'recent').exists());self.assertFalse((root/'recent/source.tar').exists())

    def test_failed_archive_copy_does_not_leave_host_tar(self):
        with tempfile.TemporaryDirectory() as tmp:
            container=runtime.Container(tmp,{})
            def command(args,*a,**kw):
                if args[0]=='git': Path(tmp,'source.tar').write_bytes(b'archive')
                return {'status':'passed','exitCode':0}
            with mock.patch.object(container,'command',side_effect=command), \
                    mock.patch.object(container,'copy',side_effect=runtime.ExecutionError('copy failed')):
                with self.assertRaises(runtime.ExecutionError):
                    runtime._provision(container,'head',tmp,{}, {'setup':''}, False)
            self.assertFalse(Path(tmp,'source.tar').exists())

    def test_failed_screenshot_copy_removes_partial_archive(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(runtime.subprocess,'run',return_value=SimpleNamespace(returncode=1)):
            with self.assertRaises(runtime.ExecutionError):
                runtime._screenshots(SimpleNamespace(name='container'),Path(tmp),tmp)
            self.assertFalse(Path(tmp,'screenshots.tar').exists())

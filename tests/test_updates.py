"""Updates preserve user data, stop at PR boundaries and roll back as a pair."""
import hashlib
import json
import os
from pathlib import Path
import plistlib
import runpy
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

from lib import paths, store, updates, github, checkpoints, drafts, insights, selection, issues

ROOT = Path(__file__).resolve().parents[1]


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        (self.home / 'state').mkdir()
        (self.home / 'VERSION').write_text('1.1.0\n')
        for obj, key, value in ((paths, 'HOME', self.home), (store, 'STATE_DIR', str(self.home / 'state')),
                                (store, 'LOCK', str(self.home / 'state/tick.lock')),
                                (store, 'DRAFTS', str(self.home / 'state/drafts'))):
            patch = mock.patch.object(obj, key, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(store.release_lock)
        app_probe = mock.patch.object(updates, 'check_app')
        app_probe.start()
        self.addCleanup(app_probe.stop)

    def release(self, version='1.2.0'):
        root = self.home / 'state/releases' / version
        root.mkdir(parents=True)
        for name, content in {'review-bot': 'print("healthy")\n', 'VERSION': version,
                              'lib/updates.py': '# updated\n', 'app/ReviewBot.app/Contents/MacOS/ReviewBot': 'new app'}.items():
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        info = root / 'app/ReviewBot.app/Contents/Info.plist'
        info.write_bytes(plistlib.dumps({'CFBundleShortVersionString': version,
                                        'CFBundleIdentifier': 'com.review-bot.app'}))
        self.manifest(root, version)
        return root

    def manifest(self, root, version):
        files = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in root.rglob('*') if p.is_file() and p.name != 'release.json'}
        (root / 'release.json').write_text(json.dumps({'version': version, 'protocol': 1, 'stateSchema': 1,
                                                     'files': files}))

    def app(self):
        target = self.home / 'Applications/ReviewBot.app'
        target.mkdir(parents=True)
        (target / 'old').write_text('original app')
        return target

    def test_dirty_git_install_is_not_queued_or_overwritten(self):
        (self.home / '.git').mkdir()
        with mock.patch.object(updates, 'command', return_value=' M review-bot\n'), \
                mock.patch.object(updates, 'check') as check, mock.patch.object(updates, 'wake') as wake:
            with self.assertRaisesRegex(updates.UpdateError, '로컬 수정'):
                updates.request()
        check.assert_not_called()
        wake.assert_not_called()
        self.assertFalse(updates.pending())

    def test_modified_release_and_schema_change_are_rejected(self):
        root = self.release()
        updates.verify_tree(root)
        (root / 'review-bot').write_text('local changes')
        with self.assertRaisesRegex(updates.UpdateError, '변경되었거나'):
            updates.ensure_clean(root)
        self.manifest(root, '1.2.0')
        value = json.loads((root / 'release.json').read_text())
        value['stateSchema'] = 2
        (root / 'release.json').write_text(json.dumps(value))
        with self.assertRaisesRegex(updates.UpdateError, '마이그레이션'):
            updates.verify_tree(root)

    def test_clean_but_changed_previous_checkout_is_not_used_for_rollback(self):
        (self.home / '.git').mkdir()
        previous = {'root': str(self.home), 'identity': 'git:old-commit'}
        with mock.patch.object(updates, 'command', side_effect=['', 'new-commit']):
            with self.assertRaisesRegex(updates.UpdateError, '이후 변경'):
                updates.rollback_root(previous)

    def test_archive_cannot_write_outside_release_or_include_user_data_or_links(self):
        for name in ('../outside', '/outside', 'state/drafts/private', 'config.json', 'link'):
            with self.subTest(name=name):
                archive = self.home / 'bad.zip'
                with zipfile.ZipFile(archive, 'w') as out:
                    item = zipfile.ZipInfo(name)
                    if name == 'link':
                        item.external_attr = (stat.S_IFLNK | 0o777) << 16
                    out.writestr(item, 'payload')
                with self.assertRaises(updates.UpdateError):
                    updates.extract(archive, self.home / 'extract')

    def test_digest_mismatch_is_rejected_before_extracting_or_executing(self):
        def download(*args, **kwargs):
            kwargs['stdout'].write(b'corrupt download')
            return subprocess.CompletedProcess(args, 0)
        with mock.patch.object(updates.subprocess, 'run', side_effect=download), \
                mock.patch.object(updates, 'extract') as extract:
            with self.assertRaisesRegex(updates.UpdateError, 'SHA256'):
                updates.prepare({'version': 'v1.2.0', 'assetID': 3, 'digest': 'sha256:' + '0' * 64})
        extract.assert_not_called()

    def test_downloaded_release_is_verified_and_staged_without_switching(self):
        root = self.release()
        archive = self.home / 'download.zip'
        with zipfile.ZipFile(archive, 'w') as out:
            for path in root.rglob('*'):
                if path.is_file():
                    out.write(path, str(path.relative_to(root)))
        content = archive.read_bytes()
        def download(*args, **kwargs):
            kwargs['stdout'].write(content)
            return subprocess.CompletedProcess(args, 0)
        release = {'version': 'v1.2.0', 'assetID': 3,
                   'digest': 'sha256:' + hashlib.sha256(content).hexdigest()}
        with mock.patch.object(updates.subprocess, 'run', side_effect=download), \
                mock.patch.object(updates, 'command', return_value='healthy') as health:
            staged = updates.prepare(release)
        self.assertEqual(updates.verify_tree(staged)['version'], '1.2.0')
        health.assert_called_once()
        self.assertEqual(updates.active_root(), self.home)

    def test_only_newer_stable_release_is_available_and_checksum_is_required(self):
        release = {'tag_name': 'v1.2.0', 'assets': [{'name': updates.ASSET, 'id': 1,
                                                  'digest': 'sha256:' + 'a' * 64}]}
        with mock.patch.object(updates, 'command', return_value=json.dumps(release)):
            self.assertTrue(updates.check()['available'])
        release['tag_name'] = 'v1.0.0'
        with mock.patch.object(updates, 'command', return_value=json.dumps(release)):
            self.assertFalse(updates.check()['available'])
        release['assets'][0]['digest'] = None
        with mock.patch.object(updates, 'command', return_value=json.dumps(release)):
            with self.assertRaises(updates.UpdateError):
                updates.check()
        self.assertIn('검증 가능한', updates.status()['checkError'])

    def test_activation_health_failure_restores_app_and_engine_and_preserves_data(self):
        root, app = self.release(), self.app()
        config = self.home / 'config.json'
        config.write_text('{"account":"personal"}')
        draft = self.home / 'state/draft.json'
        draft.write_text('private draft')
        with mock.patch.object(updates, 'command', side_effect=updates.UpdateError('bad startup')):
            with self.assertRaises(updates.UpdateError):
                updates.activate(root, str(app))
        self.assertEqual(updates.active_root(), self.home)
        self.assertEqual((app / 'old').read_text(), 'original app')
        self.assertEqual(config.read_text(), '{"account":"personal"}')
        self.assertEqual(draft.read_text(), 'private draft')
        self.assertFalse(updates.read('update-transaction.json'))

    def test_activation_and_manual_rollback_restore_original_app(self):
        root, app = self.release(), self.app()
        with mock.patch.object(updates, 'command', return_value='healthy'):
            previous = updates.activate(root, str(app))
            self.assertEqual(updates.active_root(), root)
            self.assertTrue((app / 'Contents/MacOS/ReviewBot').is_file())
            updates.activate(self.home, str(app), source_app=previous['appBackup'])
        self.assertEqual(updates.active_root(), self.home)
        self.assertEqual((app / 'old').read_text(), 'original app')

    def test_interrupted_switch_restores_both_parts_from_journal(self):
        root, app = self.release(), self.app()
        backup = app.with_name('previous.app')
        app.rename(backup)
        shutil.copytree(root / 'app/ReviewBot.app', app)
        updates._point(root)
        updates.write('update-transaction.json', {'root': str(self.home), 'appPath': str(app),
                                                'appBackup': str(backup)})
        updates.recover_activation()
        self.assertEqual(updates.active_root(), self.home)
        self.assertTrue((app / 'old').exists())

    def test_cancelled_waiter_never_activates_and_releases_lock(self):
        root = self.release()
        updates.write('update-request.json', {'id': 'job', 'release': {}})
        def wait(_):
            updates.cancel()
        with mock.patch.object(updates, 'ensure_clean'), mock.patch.object(updates, 'prepare', return_value=root), \
                mock.patch.object(store, 'acquire_lock', return_value=False), \
                mock.patch.object(updates.time, 'sleep', side_effect=wait), \
                mock.patch.object(updates, 'activate') as activate:
            updates.apply_pending()
        activate.assert_not_called()
        self.assertFalse(updates.pending())
        self.assertEqual(updates.status()['phase'], 'cancelled')

    def test_cancelling_during_download_keeps_cancelled_status(self):
        root = self.release()
        updates.write('update-request.json', {'id': 'job', 'release': {}})
        def download(_):
            updates.cancel()
            return root
        with mock.patch.object(updates, 'ensure_clean'), mock.patch.object(updates, 'prepare', side_effect=download), \
                mock.patch.object(updates, 'activate') as activate:
            updates.apply_pending()
        activate.assert_not_called()
        self.assertEqual(updates.status()['phase'], 'cancelled')

    def test_package_roundtrip_contains_only_tracked_code_and_version_matched_app(self):
        root = self.release()
        (root / 'release.json').unlink()
        for name in ('config.example.json', 'README.md', '.gitignore'):
            (root / name).write_text('config.json\nstate/\napp/*.app\nlib/.secret\n' if name == '.gitignore' else '{}')
        for name in ('prompts', 'agents'):
            (root / name).mkdir()
            (root / name / 'sample.md').write_text('bundled content')
        def git(*args):
            subprocess.run(['git', '-C', str(root), '-c', 'core.hooksPath=/dev/null',
                            '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', *args],
                           check=True, capture_output=True)
        git('init')
        git('add', '.')
        git('commit', '-m', 'release')
        (root / 'config.json').write_text('PRIVATE CONFIG')
        (root / 'lib/.secret').write_text('PRIVATE SECRET')
        output = self.home / 'bundle.zip'
        packager = runpy.run_path(str(ROOT / 'scripts/package-release.py'))
        with mock.patch.object(sys, 'platform', 'linux'):
            packager['package'](root, root / 'app/ReviewBot.app', output)
        extracted = self.home / 'unpacked'
        updates.extract(output, extracted)
        updates.verify_tree(extracted)
        self.assertFalse((extracted / 'config.json').exists())
        self.assertFalse((extracted / 'lib/.secret').exists())
        self.assertEqual((extracted / 'VERSION').read_text(), '1.2.0')

    def test_pending_apply_waits_for_the_running_review_lock(self):
        root = self.release()
        updates.write('update-request.json', {'id': 'job', 'release': {}})
        with mock.patch.object(updates, 'ensure_clean'), mock.patch.object(updates, 'prepare', return_value=root), \
                mock.patch.object(store, 'acquire_lock', side_effect=[False, True]) as acquire, \
                mock.patch.object(store, 'release_lock') as release, \
                mock.patch.object(updates.time, 'sleep'), mock.patch.object(updates, 'command', return_value='healthy'):
            updates.apply_pending()
        self.assertEqual(acquire.call_count, 2)
        release.assert_called_once()
        self.assertEqual(updates.active_root(), root)
        self.assertFalse(updates.pending())

    def test_launcher_routes_new_commands_but_config_and_state_stay_in_home(self):
        # Execute real CLI processes so module globals and the exec boundary are exercised.
        for name in ('lib', 'prompts', 'agents'):
            shutil.copytree(ROOT / name, self.home / name, ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copy2(ROOT / 'review-bot', self.home / 'review-bot')
        root = self.home / 'state/releases/9.0.0'
        root.mkdir(parents=True)
        for name in ('lib', 'prompts', 'agents'):
            shutil.copytree(self.home / name, root / name)
        shutil.copy2(ROOT / 'review-bot', root / 'review-bot')
        (root / 'VERSION').write_text('9.0.0')
        (self.home / 'config.json').write_text('{"post":{"dryRun":true},"account":"keep-me"}')
        updates._point(root)
        env = dict(os.environ)
        env.pop('REVIEW_BOT_HOME', None)
        env.pop('REVIEW_BOT_NO_REDIRECT', None)
        def cli(*args):
            return subprocess.check_output([sys.executable, str(self.home / 'review-bot'), *args], env=env, text=True)
        self.assertIn('9.0.0', cli('--version'))
        cli('mode', 'draft')
        cli('pause', 'preserve data root')
        self.assertEqual(json.loads((self.home / 'config.json').read_text())['account'], 'keep-me')
        self.assertTrue((self.home / 'state/PAUSED').exists())
        self.assertFalse((root / 'config.json').exists())
        self.assertFalse((root / 'state').exists())

    def test_tick_stops_before_next_pr_when_update_arrives_during_current_pr(self):
        bot = runpy.run_path(str(ROOT / 'review-bot'))
        tick = bot['_tick']
        prs = [{'repo': 'o/r', 'number': n, 'title': 'PR'} for n in (1, 2)]
        seen = []
        def process(pr, *a, **kw):
            seen.append(pr['number'])
            updates.write('update-request.json', {'id': 'queued-during-review'})
            return {'skipped': 'fixture'}
        with mock.patch.dict(tick.__globals__, {'_gc': lambda _: None, '_log': lambda _: None,
                                                'process_pr': process, '_connect_repomind': lambda _: (None, False),
                                                '_announce_skip': lambda *a: None}), \
                mock.patch.object(store, 'load', return_value={}), \
                mock.patch.object(github, 'whoami', return_value='me'), \
                mock.patch.object(github, 'review_requests', return_value=prs), \
                mock.patch.object(github, 'sent_review_requests', return_value=([], [])), \
                mock.patch.object(github, 'assigned_issues', return_value=([], [])), \
                mock.patch.object(drafts, 'reconcile', return_value=[]), \
                mock.patch.object(checkpoints, 'watching', return_value=[]), \
                mock.patch.object(insights, 'refresh'), mock.patch.object(selection, 'refresh'), \
                mock.patch.object(issues, 'refresh'):
            tick({'repos': {'o/r': {'account': 'me'}}, 'limits': {'maxPerTick': 5}, 'notify': {'enabled': False}})
        self.assertEqual(seen, [1])


if __name__ == '__main__':
    unittest.main()

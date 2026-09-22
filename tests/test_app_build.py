"""Reject an incorrectly stamped SDK before replacing a working app bundle."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class AppBuildTests(unittest.TestCase):
    def test_wrong_linked_sdk_preserves_existing_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copyfile(Path(__file__).resolve().parents[1] / 'app/build.sh', root / 'build.sh')
            sentinel = root / 'ReviewBot.app/keep'
            sentinel.parent.mkdir()
            sentinel.write_text('working app')
            commands = root / 'commands'
            commands.mkdir()
            xcrun = commands / 'xcrun'
            xcrun.write_text('''#!/bin/sh
case "$1" in
  --sdk)
    case "$3" in
      --show-sdk-version) echo 27.0 ;;
      --show-sdk-path) echo /fixture/MacOSX27.0.sdk ;;
    esac ;;
  swift)
    case "$*" in *--show-bin-path*) echo /fixture/bin ;; esac ;;
  otool)
    echo '      cmd LC_BUILD_VERSION'
    echo '    minos 14.0'
    echo '      sdk 14.0' ;;
  *) exit 90 ;;
esac
''')
            xcrun.chmod(0o755)
            result = subprocess.run(['bash', str(root / 'build.sh')], capture_output=True, text=True,
                                    env={**os.environ, 'PATH': str(commands) + os.pathsep + os.environ['PATH']})
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('빌드 SDK 불일치', result.stderr)
            self.assertEqual(sentinel.read_text(), 'working app')

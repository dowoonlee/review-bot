#!/usr/bin/env python3
"""Package a tested, version-matched CLI and app; never uploads or publishes."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from lib import paths, updates


def package(root, app, output):
    root, app, output = Path(root), Path(app), Path(output)
    updates.ensure_clean(root)
    version = paths.version(root)
    updates.number(version)
    if sys.platform == 'darwin':
        updates.command(['codesign', '--verify', '--deep', '--strict', str(app)])
        architectures = updates.command(['lipo', '-archs', str(app / 'Contents/MacOS/ReviewBot')]).split()
        if not {'arm64', 'x86_64'}.issubset(architectures):
            raise updates.UpdateError('REVIEW_BOT_UNIVERSAL=1로 앱을 빌드하세요.')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temp:
        tree = Path(temp, 'release')
        tree.mkdir()
        # Package tracked code only: ignored local files must never enter a release.
        names = updates.command(['git', '-C', str(root), 'ls-files', '-z', '--',
                                 'review-bot', 'VERSION', 'config.example.json', 'README.md',
                                 'lib', 'prompts', 'agents']).split('\0')
        for name in filter(None, names):
            source = root / name
            if source.is_symlink():
                raise updates.UpdateError('배포 소스에 심볼릭 링크를 포함할 수 없습니다.')
            target = tree / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        shutil.copytree(app, tree / 'app/ReviewBot.app', symlinks=True)
        files = {}
        for path in sorted(tree.rglob('*')):
            if path.is_symlink():
                raise updates.UpdateError('배포 파일에 심볼릭 링크를 포함할 수 없습니다.')
            if path.is_file():
                files[str(path.relative_to(tree))] = hashlib.sha256(path.read_bytes()).hexdigest()
        (tree / 'release.json').write_text(json.dumps({
            'protocol': updates.PROTOCOL, 'stateSchema': updates.STATE_SCHEMA,
            'version': version, 'files': files}, indent=2))
        updates.verify_tree(tree)
        staged = output.with_name(output.name + '.tmp')
        try:
            with zipfile.ZipFile(staged, 'w', zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(tree.rglob('*')):
                    if path.is_file():
                        archive.write(path, str(path.relative_to(tree)))
            os.replace(staged, output)
        finally:
            staged.unlink(missing_ok=True)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix('.zip.sha256').write_text(digest + '  ' + output.name + '\n')
    return digest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', type=Path, default=ROOT / 'app/ReviewBot.app')
    parser.add_argument('--output', type=Path, default=ROOT / 'dist' / updates.ASSET)
    args = parser.parse_args()
    try:
        digest = package(ROOT, args.app, args.output)
        print(str(args.output) + '\nsha256:' + digest)
    except (updates.UpdateError, OSError, ValueError) as exc:
        parser.exit(1, str(exc) + '\n')

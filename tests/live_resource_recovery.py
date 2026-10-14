"""Opt-in Docker recovery smoke test: python tests/live_resource_recovery.py LOCAL_IMAGE_ID.

Uses only temporary containers/anonymous volumes, no host mounts or network.
"""
import json
from pathlib import Path
import select
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import resources, runtime, store


def configure(root):
    store.STATE_DIR = str(root)
    store.DRAFTS = str(root / 'drafts')
    store.LOCK = str(root / 'tick.lock')


def create(root, image, foreign=False):
    root.mkdir()
    container = runtime.Container(root, {})
    if foreign:
        container.owner['instance'] = 'other-bot-instance'
    (root / 'name').write_text(container.name)
    container.command(['docker', 'create', '--name', container.name,
                       '--label', 'review-bot.runtime=true', '--label', 'review-bot.audit=recovery',
                       '--network', 'none', '--cap-drop=ALL', '--memory', '64m',
                       '--entrypoint', '/bin/sh', '-v', '/audit'] + container.ownership_labels() +
                      [image, '-c', 'sleep 120'])
    container.command(['docker', 'start', container.name])
    return container


def inspect(name):
    result = subprocess.run(['docker', 'inspect', name], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)[0]


def child(root, image):
    configure(root)
    assert store.acquire_lock()
    container = create(root / 'crashed', image)
    data = inspect(container.name)
    print(json.dumps({'name': container.name,
                      'volumes': [m['Name'] for m in data['Mounts'] if m['Type'] == 'volume']}), flush=True)
    import time
    time.sleep(120)


def main(image):
    with tempfile.TemporaryDirectory(prefix='review-bot-recovery-test-') as tmp:
        root = Path(tmp)
        configure(root)
        process = subprocess.Popen([sys.executable, __file__, '--child', tmp, image],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            if not select.select([process.stdout], [], [], 30)[0]:
                raise RuntimeError('fixture startup timeout')
            line = process.stdout.readline()
            if not line:
                raise RuntimeError(process.stderr.read()[-2000:])
            dead = json.loads(line)
            process.kill();process.wait(timeout=10)
            assert store.acquire_lock(), 'SIGKILL lock recovery failed'
            live = create(root / 'live', image)
            foreign = create(root / 'foreign', image, foreign=True)
            report = resources.sweep_containers()
            assert report['removed'] == [dead['name']] and not report['errors'], report
            assert inspect(live.name) and inspect(foreign.name)
            for volume in dead['volumes']:
                assert subprocess.run(['docker', 'volume', 'inspect', volume], capture_output=True).returncode != 0
            print(json.dumps({'SIGKILL_lock_recovered': True, 'orphan_container_removed': True,
                              'anonymous_volumes_removed': len(dead['volumes']),
                              'live_and_foreign_containers_preserved': True}))
        finally:
            if process.poll() is None:
                process.kill();process.wait(timeout=10)
            cleanup_errors = []
            for namefile in root.glob('*/name'):
                name = namefile.read_text()
                result = subprocess.run(['docker', 'rm', '-f', '-v', name], capture_output=True, text=True, timeout=30)
                if result.returncode and 'No such container' not in result.stderr:
                    cleanup_errors.append(name)
            store.release_lock()
            process.stdout.close();process.stderr.close()
            if cleanup_errors:
                raise RuntimeError('Fixture cleanup failed: ' + ', '.join(cleanup_errors))


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == '--child':
        child(Path(sys.argv[2]), sys.argv[3])
    elif len(sys.argv) == 2:
        main(sys.argv[1])
    else:
        raise SystemExit('usage: python tests/live_resource_recovery.py LOCAL_IMAGE_ID')

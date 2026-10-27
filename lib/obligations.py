"""Required checks derived from the head diff, not from a generated plan.

What a PR must prove follows from what it changes. The planner still writes the
repository-specific commands, but it cannot leave out a check this module demands,
and approval cannot pass without a passing test that covers it. Because the checks
are recomputed from the current head every run, a new commit neither inherits stale
obligations nor escapes new ones.
"""
import posixpath
import re
import shlex
import subprocess
from pathlib import Path

MIGRATION = 'migration-roundtrip'
CONFIG = 'config-contract'
VISUAL = 'visual'

MIGRATION_PATH = re.compile(r'(^|/)(alembic|migrations)/versions/[^/]+\.py$')
_REVISION = re.compile(r'^revision(?:\s*:\s*[^=]+)?\s*=\s*(["\'])([0-9a-zA-Z_]+)\1', re.M)
_DOWN_REVISION = re.compile(r'^down_revision(?:\s*:\s*[^=]+)?\s*=\s*(.+)$', re.M)
_IDENTIFIER = re.compile(r'["\']([0-9a-zA-Z_]+)["\']')
# Deployment and runtime configuration. Repositories usually pin these with static
# contract tests that name the file; those tests are what proves such a change.
CONFIG_PATH = re.compile(r'^(k8s|helm|charts|deploy)/|(^|/)(nginx[^/]*\.conf|docker-compose[^/]*\.ya?ml|Dockerfile[^/]*)$'
                         r'|^\.github/workflows/')
TEST_FILE = re.compile(r'(^|/)(test_[^/]+\.py|[^/]+_test\.py|[^/]+\.(test|spec)\.(js|jsx|ts|tsx|mjs))$')
MAX_TEST_BYTES = 400_000


def visual_required_paths(files):
    """Obvious production UI changes cannot be waived by a generated plan."""
    excluded = {'docs', 'tests', 'test', '__tests__', '__fixtures__', 'fixtures',
                'e2e', 'mock-e2e', 'browser-tests', '__snapshots__'}
    extensions = {'.tsx', '.jsx', '.vue', '.svelte', '.css', '.scss', '.sass', '.less', '.html', '.svg'}
    result = []
    for item in files:
        path = Path(item.get('filename') or '')
        if (path.suffix.lower() not in extensions or
                excluded.intersection(p.lower() for p in path.parts) or
                re.search(r'\.(test|spec|stories)\.', path.name, re.I)):
            continue
        if item.get('status') == 'renamed' and item.get('additions') == item.get('deletions') == 0:
            continue
        result.append(str(path))
    return result


def migration_revisions(files, cwd):
    """New or changed Alembic revisions in the worktree, with the revisions they build on."""
    root = Path(cwd).resolve()
    found = []
    for item in files:
        name = item.get('filename') or ''
        if item.get('status') == 'removed' or not MIGRATION_PATH.search(name):
            continue
        path = root / name
        entry = {'path': name, 'revision': None, 'downRevisions': []}
        if not path.is_symlink() and path.is_file() and path.resolve().is_relative_to(root):
            text = path.read_text(errors='replace')[:20000]
            match = _REVISION.search(text)
            if match:
                entry['revision'] = match.group(2)
            down = _DOWN_REVISION.search(text)
            if down:
                entry['downRevisions'] = _IDENTIFIER.findall(down.group(1))
        found.append(entry)
    return found


def _tracked(cwd):
    proc = subprocess.run(['git', '-C', str(cwd), 'ls-files', '-z'], capture_output=True, text=True, timeout=30)
    return [] if proc.returncode else [p for p in proc.stdout.split('\0') if p]


def config_contract_tests(files, cwd, tracked=None):
    """Changed config files and the head's test files that name them by exact path."""
    changed = [item['filename'] for item in files
               if item.get('status') != 'removed' and CONFIG_PATH.search(item.get('filename') or '')]
    if not changed:
        return [], []
    root = Path(cwd).resolve()
    tests = []
    for name in sorted(tracked if tracked is not None else _tracked(cwd)):
        if not TEST_FILE.search(name):
            continue
        path = root / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_TEST_BYTES:
            continue
        text = path.read_text(errors='replace')
        if any(f in text for f in changed):
            tests.append(name)
    return changed, tests


def required_checks(files, cwd):
    checks = []
    changed, tests = config_contract_tests(files, cwd)
    if tests:
        checks.append({
            'kind': CONFIG, 'paths': changed, 'tests': tests,
            'needs': '바뀐 설정 파일을 경로로 직접 가리키는 테스트 파일 전부를 required 테스트로 실행하세요. '
                     '명령 인자에 각 파일(또는 그 상위 디렉터리)을 넣고 covers=["%s"] 를 적으세요.' % CONFIG})
    migrations = migration_revisions(files, cwd)
    if migrations:
        checks.append({
            'kind': MIGRATION,
            'paths': [m['path'] for m in migrations],
            'revisions': [m['revision'] for m in migrations if m['revision']],
            'downRevisions': sorted({r for m in migrations for r in m['downRevisions']}),
            'needs': '일회용 Postgres에서 새 리비전 아래로 downgrade 한 뒤 upgrade head 까지 되돌리는 왕복. '
                     'services=["postgres"], requires=["postgres"], required=true, covers=["%s"]. '
                     '봇이 이 명령에만 ALLOW_DESTRUCTIVE_MIGRATION=1 을 넣습니다.' % MIGRATION})
    visual = visual_required_paths(files)
    if visual:
        checks.append({'kind': VISUAL, 'paths': visual,
                       'needs': 'visualApplicable=true 와 실제 화면을 조작하는 browser 계획.'})
    return checks


def covers(test, kind):
    return kind in (test.get('covers') or [])


def _covering_tests(plan, check):
    return [t for t in plan.get('tests', []) if covers(t, check['kind']) and t.get('required', True)]


def command_paths(command):
    """Repository-relative path arguments of a shell command, following `cd`."""
    paths, directory = set(), ''
    for segment in re.split(r'\s*(?:&&|;|\|\|)\s*', command):
        try:
            words = shlex.split(segment)
        except ValueError:
            words = segment.split()
        if words[:1] == ['cd'] and len(words) > 1:
            directory = posixpath.normpath(posixpath.join(directory, words[1].replace('/work', '', 1).lstrip('/')))
            continue
        for word in words:
            if word.startswith('-') or '=' in word:
                continue
            word = word.split('::', 1)[0]
            target = word.replace('/work/', '', 1) if word.startswith('/work/') else posixpath.join(directory, word)
            paths.add(posixpath.normpath(target).lstrip('/'))
    return paths


def _runs(test_file, paths):
    return any(test_file == p or p == '.' or test_file.startswith(p.rstrip('/') + '/') for p in paths)


def plan_gaps(plan, checks):
    """Why a plan does not satisfy the derived checks; empty when it does."""
    gaps = []
    for check in checks:
        if check['kind'] == VISUAL:
            continue  # Enforced through visualApplicable and the browser plan.
        tests = _covering_tests(plan, check)
        label = '%s (%s)' % (check['kind'], ', '.join(check['paths']))
        if not tests:
            gaps.append('%s 를 covers 로 선언한 required 테스트가 없습니다' % label)
            continue
        if check['kind'] == CONFIG:
            paths = set().union(*(command_paths(t.get('command') or '') for t in tests))
            missing = [f for f in check.get('tests', []) if not _runs(f, paths)]
            if missing:
                gaps.append('%s: 이 테스트 파일이 명령에 없습니다: %s' % (label, ', '.join(missing[:12]) +
                                                              (' 외 %d개' % (len(missing) - 12) if len(missing) > 12 else '')))
        if check['kind'] == MIGRATION:
            identifiers = set(check.get('revisions', [])) | set(check.get('downRevisions', []))
            for test in tests:
                command = test.get('command') or ''
                if 'postgres' not in plan.get('services', []) or 'postgres' not in (test.get('requires') or []):
                    gaps.append('%s: services 와 requires 에 postgres 가 필요합니다' % label)
                if 'downgrade' not in command or 'upgrade' not in command:
                    gaps.append('%s: 명령에 downgrade 와 upgrade 가 모두 있어야 합니다' % label)
                if identifiers and not any(i in command for i in identifiers):
                    gaps.append('%s: 명령이 새 리비전이나 그 down_revision(%s) 을 가리켜야 합니다'
                                % (label, ', '.join(sorted(identifiers))))
    return gaps


def test_environment(test, container_environment):
    """Extra environment for one test command; never for setup or app start."""
    if covers(test, MIGRATION) and container_environment.get('DB_HOST'):
        # The guard protects shared databases; this one is disposable and loopback-only.
        return {'ALLOW_DESTRUCTIVE_MIGRATION': '1'}
    return {}


def approval_gaps(evidence, satisfied):
    """Derived checks without a passing, covering test block approval."""
    gaps = []
    tests = evidence.get('tests') or []
    for check in evidence.get('requiredChecks') or []:
        if check['kind'] == VISUAL:
            continue
        covering = [t for t in tests if covers(t, check['kind'])]
        if not any(satisfied(t) for t in covering):
            state = ', '.join('%s: %s' % (t.get('name'), t.get('status')) for t in covering) or '검증 항목 없음'
            gaps.append('%s (%s) — %s' % (check['kind'], ', '.join(check['paths']), state))
    return gaps

"""Keep validation obligations intact while a generated execution plan is repaired."""
import copy
import hashlib
import re


class ContractError(ValueError):
    pass


# The sandbox has no Docker daemon. Matches docker-py's missing-socket error and the
# DockerException raised by lib/docker_shim once a real API call is attempted.
DOCKER_UNAVAILABLE = re.compile(
    r"DockerException: Error while fetching server API version: \('Connection aborted\.', "
    r"FileNotFoundError|review-bot: the validation sandbox provides no Docker daemon")


BROWSER_MISSING = r"browserType\.launch: Executable doesn't exist at"


def docker_unavailable_case(case):
    return bool(DOCKER_UNAVAILABLE.search(case.get('message') or ''))


def docker_unavailable(result):
    """Every failure is the missing Docker daemon, not an assertion about the code."""
    if result.get('reportError'):
        return False
    failed = [c for c in result.get('cases') or [] if c.get('status') == 'failed']
    if failed:
        return all(docker_unavailable_case(c) for c in failed)
    return bool(DOCKER_UNAVAILABLE.search(result.get('output') or ''))


def check_id(test):
    return test.get('id') or 'test-' + hashlib.sha256(
        (test.get('name', '') + '\0' + test.get('command', '')).encode()).hexdigest()[:12]


def satisfied(test):
    if test.get('status') != 'passed':
        return False
    if not test.get('required', True) or not test.get('cases'):
        return True
    required = required_cases(test)
    passed = {c['id'] for c in test['cases'] if c['status'] == 'passed'}
    return bool(passed) and required <= passed


def executed(case):
    """A case that actually exercised this PR's code and said something about it.

    A case the suite skipped by design (pytest.skip with its reason) or one that fails
    on base for the same cause was not run against the change; it is reported, not
    required. A base failure with a different cause may be the PR's doing (#7594).
    """
    status = case.get('status')
    if status in ('passed', 'failed'):
        return True
    if status != 'baseline' or not case.get('baselineDiffers'):
        return False
    # A flag recorded before incidental differences were normalized is re-checked
    # against the kept messages, so an older run's evidence carries the same meaning.
    head, base = case.get('message'), case.get('baselineMessage')
    return not (head and base and without_incidentals(head) == without_incidentals(base))


def without_incidentals(text):
    """Object addresses, per-run temporary directory names and the middle of a value pytest
    elided with "..." differ between two runs of the same failing test; none of them says
    the cause differs."""
    text = re.sub(r' at 0x[0-9a-fA-F]+', ' at <address>', text)
    text = re.sub(r'/tmp/\S+', lambda path: re.sub(r'\d+', 'N', path.group()), text)
    return re.sub(r'\S*\.\.\.\S*', '<elided>', text)


def required_cases(test):
    """Case ids the test must pass: the planner's explicit scope, else every executed case."""
    cases = test.get('cases') or []
    requested = test.get('requiredCases')
    if not requested:
        return {case['id'] for case in cases if executed(case)}
    lookup = {}
    for case in cases:
        lookup.setdefault(case['id'], set()).add(case['id'])
        if case.get('baseId'):
            lookup.setdefault(case['baseId'], set()).add(case['id'])
    return set().union(*(lookup.get(identity, {identity}) for identity in requested))


def failure_kind(result, browser=False):
    if result.get('status') in ('passed', 'baseline'):
        return None
    if result.get('failureKind'):
        return result['failureKind']
    if result.get('reportError'):
        return 'plan'
    output = result.get('output') or ''
    if browser:
        if re.search(r"browserType\.launch|" + BROWSER_MISSING, output):
            return 'infrastructure'
        if re.search(r'AssertionError', output):
            return 'test'
        if re.search(r'strict mode violation|TimeoutError|is not a function|is not defined|SyntaxError', output):
            return 'plan'
    if docker_unavailable(result):
        return 'environment'
    # A test runner whose browser is not installed never reached the product.
    failed = [c for c in result.get('cases') or [] if c.get('status') == 'failed']
    if failed and all(re.search(BROWSER_MISSING, c.get('message') or '') for c in failed):
        return 'infrastructure'
    # A failure inside a test is not an infrastructure failure just because its
    # assertion/traceback mentions TimeoutError or ConnectionError.
    if any(c.get('status') == 'failed' for c in result.get('cases', [])):
        return 'test'
    if re.search(r'No test files found|No tests found|no tests ran|command not found|Failed to spawn', output, re.I):
        return 'plan'
    if re.search(r'heap out of memory|ENOTFOUND|EAI_AGAIN', output, re.I):
        return 'infrastructure'
    return 'test'


def obligations(previous):
    previous = previous or {}
    saved = {}
    for test in previous.get('requirements') or (previous.get('plan') or {}).get('tests', []):
        if previous.get('requirements') or test.get('required', True):
            saved[check_id(test)] = copy.deepcopy(test)
    for test in previous.get('tests', []):
        if test.get('status') == 'failed' and failure_kind(test) == 'test':
            # Even a broad regression run cannot disappear after discovering a failure.
            saved.setdefault(check_id(test), copy.deepcopy(test))
        identity = check_id(test)
        if identity in saved and test.get('cases') and saved[identity].get('required', True):
            # Once concrete cases have been observed, selecting fewer cases is not a repair.
            # A saved list that names every observed case is the whole run, not a planner's
            # subset: it is re-derived so a designed skip or a same-cause base failure
            # recorded under an older rule does not stay required.
            listed = saved[identity].get('requiredCases')
            if listed and {c['id'] for c in test['cases']} <= set(listed):
                saved[identity].pop('requiredCases', None)
                listed = None
            observed = sorted(required_cases({'cases': test['cases']}))
            if observed:
                saved[identity]['requiredCases'] = listed or observed
    return list(saved.values())


def carry(previous):
    """What a PR must prove outlives a bot change; outcomes from older bot code do not."""
    required = obligations(previous)
    if not required:
        return None
    scenarios = ((previous or {}).get('plan') or {}).get('scenarios') or []
    return {'requirements': required, 'plan': {'scenarios': list(scenarios)}}


def command_core(command):
    """The command without environment assignments, so `export X=1 && cmd` equals `cmd`."""
    assignment = r'(export\s+)?[A-Za-z_][A-Za-z0-9_]*=\S*'
    segments = []
    for segment in re.split(r'\s*&&\s*', command.strip()):
        segment = re.sub(r'^(%s\s+)+' % assignment, '', segment).strip()
        if segment and not re.fullmatch(assignment, segment):
            segments.append(re.sub(r'\s+', ' ', segment))
    return ' && '.join(segments)


def repeated_plan_failures(plan, previous=None):
    """Planned commands that already failed as the bot's own command mistake, unchanged."""
    failed = {check_id(t): t for t in (previous or {}).get('tests', [])
              if t.get('status') == 'failed' and failure_kind(t) == 'plan'}
    return [test for test in plan.get('tests', [])
            if test.get('id') in failed and
            command_core(test.get('command', '')) == command_core(failed[test['id']].get('command', '')) and
            test.get('report') == failed[test['id']].get('report')]


def preserve(plan, previous=None):
    plan = copy.deepcopy(plan)
    old = {check_id(t): t for t in obligations(previous)}
    results = {check_id(t): t for t in (previous or {}).get('tests', [])}
    checks, used, extra = [], set(), []
    for test in plan['tests']:
        matches = [key for key, item in old.items() if item['command'] == test['command']]
        if not matches:
            matches = [key for key, item in old.items() if item['name'] == test['name']]
        identity = test.get('id') or (matches[0] if len(matches) == 1 else check_id(test))
        if identity in used:
            raise ContractError('테스트 id가 중복됩니다: ' + identity)
        used.add(identity)
        test['id'] = identity
        original = old.get(identity)
        if original:
            outcome = results.get(identity) or {}
            changed = any(test.get(k) != original.get(k) for k in ('command', 'report'))
            if changed and (satisfied(outcome) or
                            (outcome.get('status') == 'failed' and failure_kind(outcome) == 'test')):
                test = {k: original[k] for k in ('id', 'name', 'command', 'report', 'required', 'requiredCases', 'scopeReason', 'requires', 'covers') if k in original}
                test['id'] = identity
            elif changed and not test.get('repairReason'):
                # An unexplained rewrite neither replaces the obligation nor discards the
                # whole run: the original command stays, the rewrite runs beside it —
                # unless it only adds environment variables, which is the same check twice.
                if (command_core(test.get('command', '')) != command_core(original.get('command', '')) or
                        test.get('report') != original.get('report')):
                    extra.append({k: v for k, v in test.items() if k != 'id'})
                test = {k: original[k] for k in ('name', 'command', 'report', 'required', 'requiredCases', 'scopeReason', 'requires', 'covers') if k in original}
                test['id'] = identity
            test['required'] = original.get('required', True)
            if original.get('requires'):
                test['requires'] = list(dict.fromkeys(original['requires'] + test.get('requires', [])))
            if original.get('scopeReason'):
                test['scopeReason'] = original['scopeReason']
            if original.get('requiredCases'):
                test['requiredCases'] = list(dict.fromkeys(original['requiredCases'] + (test.get('requiredCases') or [])))
            else:
                test.pop('requiredCases', None)  # "all cases" cannot become a subset.
        checks.append(test)
    for test in extra:
        identity = check_id(test)
        while identity in used or identity in old:
            identity += '-rewrite'
        used.add(identity)
        checks.append(dict(test, id=identity))
    for identity, original in old.items():
        if identity not in used:
            checks.append({**{k: original[k] for k in ('name', 'command', 'report', 'required', 'requiredCases', 'scopeReason', 'requires', 'covers') if k in original},
                           'id': identity})
    plan['tests'] = checks
    if any(t.get('required', True) for t in checks):
        plan['testsApplicable'] = True
    prior_scenarios = ((previous or {}).get('plan') or {}).get('scenarios') or []
    plan['scenarios'] = list(dict.fromkeys(plan['scenarios'] + prior_scenarios))
    required = [copy.deepcopy(t) for t in checks if t.get('required', True) or t['id'] in old]
    return plan, required

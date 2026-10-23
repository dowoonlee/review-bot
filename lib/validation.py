"""Keep validation obligations intact while a generated execution plan is repaired."""
import copy
import hashlib
import re


class ContractError(ValueError):
    pass


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
    return required <= passed


def required_cases(test):
    cases = test.get('cases') or []
    requested = test.get('requiredCases')
    if not requested:
        return {case['id'] for case in cases}
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
        if re.search(r'AssertionError', output):
            return 'test'
        if re.search(r'strict mode violation|TimeoutError|is not a function|is not defined|SyntaxError', output):
            return 'plan'
    # A failure inside a test is not an infrastructure failure just because its
    # assertion/traceback mentions TimeoutError or ConnectionError.
    if any(c.get('status') == 'failed' for c in result.get('cases', [])):
        return 'test'
    if re.search(r'No test files found|no tests ran|command not found', output, re.I):
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
            saved[identity]['requiredCases'] = saved[identity].get('requiredCases') or [c['id'] for c in test['cases']]
    return list(saved.values())


def preserve(plan, previous=None):
    plan = copy.deepcopy(plan)
    old = {check_id(t): t for t in obligations(previous)}
    results = {check_id(t): t for t in (previous or {}).get('tests', [])}
    checks, used = [], set()
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
                test = {k: original[k] for k in ('id', 'name', 'command', 'report', 'required', 'requiredCases', 'scopeReason') if k in original}
                test['id'] = identity
            elif changed and not test.get('repairReason'):
                raise ContractError('필수 테스트 명령을 보완할 때 id를 유지하고 repairReason을 적으세요: ' + identity)
            test['required'] = original.get('required', True)
            if original.get('scopeReason'):
                test['scopeReason'] = original['scopeReason']
            if original.get('requiredCases'):
                test['requiredCases'] = list(dict.fromkeys(original['requiredCases'] + (test.get('requiredCases') or [])))
            else:
                test.pop('requiredCases', None)  # "all cases" cannot become a subset.
        checks.append(test)
    for identity, original in old.items():
        if identity not in used:
            checks.append({**{k: original[k] for k in ('name', 'command', 'report', 'required', 'requiredCases', 'scopeReason') if k in original},
                           'id': identity})
    plan['tests'] = checks
    if any(t.get('required', True) for t in checks):
        plan['testsApplicable'] = True
    prior_scenarios = ((previous or {}).get('plan') or {}).get('scenarios') or []
    plan['scenarios'] = list(dict.fromkeys(plan['scenarios'] + prior_scenarios))
    required = [copy.deepcopy(t) for t in checks if t.get('required', True) or t['id'] in old]
    return plan, required

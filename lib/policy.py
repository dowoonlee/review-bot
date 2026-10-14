"""Merge decisions: priority describes impact; disposition describes required action."""

DISPOSITIONS = ('required', 'optional', 'uncertain')


def disposition(finding):
    value = finding.get('disposition')
    if value in DISPOSITIONS and str(finding.get('dispositionReason') or '').strip():
        return value
    # Old or incomplete findings never acquire permission to approve by omission.
    return 'uncertain' if finding.get('severity') in ('blocking', 'suggestion') else 'optional'


def finding_block(event, findings):
    actions = [disposition(f) for f in findings]
    if event == 'APPROVE' and any(a != 'optional' for a in actions):
        return '머지 전 수정 또는 확인이 필요한 지적이 남아 있어 승인하지 않습니다.'
    if event == 'REQUEST_CHANGES' and 'required' not in actions:
        return '재현 조건과 영향이 확인된 수정 필요 지적이 없어 사람 확인으로 남깁니다.'
    return ''


def coverage_block(payload):
    coverage = payload.get('coverage') or {}
    if coverage.get('unavailable'):
        return '확인하지 못한 diff가 있어 승인하지 않습니다.'
    errors = payload.get('errors') or []
    exceptions = payload.get('coverageExceptions') or {}
    tasks = (payload.get('_meta') or {}).get('agents') or {}
    for error in errors:
        tag = error.split(':', 1)[0]
        exception = exceptions.get(tag) or {}
        if not isinstance(exception, dict):
            return '보완 검토 근거 형식이 올바르지 않습니다.'
        covered_by = exception.get('coveredBy') or []
        if (not exception.get('reason') or not isinstance(covered_by, list) or not covered_by or
                any(not isinstance(t, str) for t in covered_by) or
                any(t not in tasks or tasks[t].get('error') or tasks[t].get('salvaged')
                    for t in covered_by)):
            return '실패한 리뷰의 필수 검토 범위를 보완하지 못해 승인하지 않습니다.'
    if coverage and not coverage.get('complete') and not errors:
        return '미완료 검토 범위가 있어 승인하지 않습니다.'
    return ''


def decision(judge_event, final_event, findings, payload, reason='', guard='', ci=None,
             environment=None, judge_failed=False):
    """Machine-readable explanation of the final decision, including every known blocker."""
    from lib import runtime
    reasons = []
    def add(code, message, source):
        reasons.append({'code': code, 'message': message, 'source': source})
    if environment and not environment['ok']:
        add('ENVIRONMENT_UNAVAILABLE', environment['reason'], 'preflight')
    for action, code, label in [('required', 'DEFECT', '머지 전 수정 필요'),
                                ('uncertain', 'HUMAN_REVIEW', '사람 확인 필요')]:
        group = [f for f in findings if disposition(f) == action]
        if group:
            add(code, '%s %d건: %s' % (label, len(group), ', '.join(f.get('title', '') for f in group)), 'finding')
    if ci in ('pending', 'failure'):
        add('CI_PENDING' if ci == 'pending' else 'CI_FAILED',
            'CI가 진행 중입니다.' if ci == 'pending' else 'CI가 실패했습니다.', 'ci')
    missing = coverage_block(payload)
    if missing:
        add('REVIEW_INCOMPLETE', missing, 'review')
    missing = runtime.approval_block(payload.get('runtime'))
    if missing:
        add('VALIDATION_INCOMPLETE', missing, 'runtime')
    if judge_failed:
        add('JUDGE_FAILED', guard or '최종 판정을 생성하지 못했습니다.', 'judge')
    if guard and not reasons:
        add('POLICY_GUARD', guard, 'policy')
    if final_event == 'COMMENT' and not reasons:
        add('HUMAN_REVIEW', reason or '최종 판단에 사람의 확인이 필요합니다.', 'judge')
    return {'judgeEvent': judge_event, 'finalEvent': final_event, 'judgeReason': reason,
            'summary': guard or (reasons[0]['message'] if reasons and final_event != judge_event else reason) or reason,
            'reasons': reasons}


def revise_decision(previous, final_event, code, message, source):
    result = dict(previous or {}, finalEvent=final_event, summary=message)
    result['reasons'] = [r for r in result.get('reasons', []) if r['code'] != code]
    result['reasons'].append({'code': code, 'message': message, 'source': source})
    return result

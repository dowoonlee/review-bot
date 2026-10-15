import copy
import json
import tempfile
import unittest
from unittest.mock import patch
from lib import issues, insight_api, github, store, selection, cleanup

REGISTERED = {'enabled': True, 'model': 'jev-latest', 'registration': 'generation1', 'keyReady': True}
ISSUE = {'repo': 'org/repo', 'number': 7, 'updatedAt': 'now', 'priority': 'Low', 'account': 'me'}
CFG = {'repos': {'org/repo': {}, 'org/off': {'enabled': False}}}
RESULT = {'state': 'actionable', 'urgency': 1.8, 'confidence': 0.9, 'inputTokens': 10, 'outputTokens': 2}
CONTEXT = {'title': 'Fix login', 'sources': [{'id': 'issue', 'body': 'Production login fails', 'url': 'https://github.com/org/repo/issues/7'}],
           'fields': {'Priority': 'Low'}, 'incomplete': False}

SEARCH_NODE = {
    'number': 7, 'title': 'Fix login', 'url': 'https://github.com/org/repo/issues/7',
    'createdAt': '2026-10-01T00:00:00Z', 'updatedAt': '2026-10-02T00:00:00Z',
    'repository': {'nameWithOwner': 'org/repo'}, 'author': {'login': 'them'},
    'labels': {'nodes': [{'name': 'project:CO'}, {'name': 'company:공통'}]},
    'milestone': {'title': 'Sprint 12', 'dueOn': '2026-10-30T00:00:00Z'},
    'comments': {'totalCount': 3}, 'issueType': {'name': 'Bug'},
    'issueFieldValues': {'nodes': [
        {'optionId': 'OPT_HIGH', 'field': {'name': 'Priority', 'options': [
            {'id': 'OPT_URGENT', 'name': 'Urgent'}, {'id': 'OPT_HIGH', 'name': 'High'}]}},
        {'value': '2026-10-20', 'field': {'name': 'Target date'}},
    ]},
}


class IssueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for name, value in {'STATE_DIR': self.temp.name, 'DRAFTS': self.temp.name + '/drafts',
                            'REVIEWED': self.temp.name + '/reviewed.json'}.items():
            p = patch.object(store, name, value); p.start(); self.addCleanup(p.stop)

    def refresh(self, issue=ISSUE, context=CONTEXT, registered=REGISTERED, result=RESULT):
        with patch.object(selection, 'registration', return_value=registered), \
             patch.object(github, 'issue_context', return_value=copy.deepcopy(context)), \
             patch.object(insight_api, 'call', return_value=result) as call:
            issues.refresh([('me', dict(issue))], CFG, lambda _: None)
            return call.call_count

    def test_search_rows_resolve_priority_option_names_without_hardcoded_ids(self):
        page = {'data': {'search': {'issueCount': 1, 'pageInfo': {'hasNextPage': False},
                                    'nodes': [SEARCH_NODE]}}}
        with patch.object(github, '_gh_json', return_value=page):
            rows, warnings = github.assigned_issues(account='me')
        self.assertEqual(warnings, [])
        row = rows[0]
        self.assertEqual((row['repo'], row['number'], row['priority'], row['targetDate']), ('org/repo', 7, 'High', '2026-10-20'))
        self.assertEqual(row['labels'], ['project:CO', 'company:공통'])
        self.assertEqual((row['milestone'], row['type'], row['comments'], row['account']), ('Sprint 12', 'Bug', 3, 'me'))

    def test_search_stops_at_cap_with_a_warning_instead_of_silently_truncating(self):
        def page(cursor):
            return {'data': {'search': {'issueCount': 500, 'pageInfo': {'hasNextPage': True, 'endCursor': cursor},
                                        'nodes': [dict(SEARCH_NODE, number=n) for n in range(1, 51)]}}}
        pages = iter([page('c1'), page('c2'), page('c3'), page('c4'), page('c5')])
        with patch.object(github, '_gh_json', side_effect=lambda *a, **k: next(pages)):
            rows, warnings = github.assigned_issues()
        self.assertEqual(len(rows), github.ASSIGNED_ISSUES_CAP)
        self.assertEqual(len(warnings), 1)

    def test_rank_prefers_ready_jev_then_priority_field_then_medium(self):
        self.assertEqual(issues.rank(ISSUE, {'status': 'ready', 'urgency': 1.8}), {'score': 1.8, 'source': 'jev'})
        self.assertEqual(issues.rank(ISSUE, {'status': 'stale', 'urgency': 1.8}), {'score': 0.5, 'source': 'priority'})
        self.assertEqual(issues.rank(dict(ISSUE, priority='Urgent'), None), {'score': 2.0, 'source': 'priority'})
        self.assertEqual(issues.rank(dict(ISSUE, priority=None), None), {'score': 1.0, 'source': 'none'})

    def test_only_watched_repos_are_classified_and_unwatched_show_no_jev(self):
        with patch.object(selection, 'registration', return_value=REGISTERED), \
             patch.object(github, 'issue_context') as context, patch.object(insight_api, 'call') as call:
            issues.refresh([('me', dict(ISSUE, repo='org/off')), ('me', dict(ISSUE, repo='org/other'))], CFG, lambda _: None)
            context.assert_not_called(); call.assert_not_called()
        self.assertIsNone(issues.view({}, REGISTERED, 'me', dict(ISSUE, repo='org/other'), CFG))
        self.assertIsNone(issues.view({}, {'enabled': False}, 'me', ISSUE, CFG))
        self.assertEqual(issues.view({}, REGISTERED, 'me', ISSUE, CFG), {'status': 'pending'})

    def test_unregistered_never_calls_helper(self):
        self.assertEqual(self.refresh(registered={'enabled': False}), 0)
        self.assertFalse(issues.path().exists())

    def test_unchanged_issue_skips_fetch_and_changed_issue_reclassifies(self):
        self.assertEqual(self.refresh(), 1)
        with patch.object(selection, 'registration', return_value=REGISTERED), \
             patch.object(github, 'issue_context') as context, patch.object(insight_api, 'call') as call:
            issues.refresh([('me', dict(ISSUE))], CFG, lambda _: None)
            context.assert_not_called(); call.assert_not_called()
        # 갱신 시각만 바뀌고 내용이 같으면 (라벨만 건드린 경우) 조회는 하되 Jev 는 다시 부르지 않는다.
        self.assertEqual(self.refresh(issue=dict(ISSUE, updatedAt='later')), 0)
        self.assertEqual(issues.view(issues.load(), REGISTERED, 'me', dict(ISSUE, updatedAt='later'), CFG)['status'], 'ready')
        changed = copy.deepcopy(CONTEXT); changed['sources'][0]['body'] = 'now fixed'
        self.assertEqual(self.refresh(issue=dict(ISSUE, updatedAt='later2'), context=changed), 1)
        view = issues.view(issues.load(), REGISTERED, 'me', dict(ISSUE, updatedAt='later2'), CFG)
        self.assertEqual((view['status'], view['state'], view['urgency']), ('ready', 'actionable', 1.8))
        self.assertEqual(issues.view(issues.load(), REGISTERED, 'me', dict(ISSUE, updatedAt='later3'), CFG)['status'], 'stale')
        self.assertEqual(issues.view(issues.load(), dict(REGISTERED, registration='g2'), 'me', dict(ISSUE, updatedAt='later2'), CFG)['status'], 'pending')

    def test_uncertain_incomplete_and_invalid_answers(self):
        self.assertEqual(issues.decision(dict(RESULT, confidence=0.5))['status'], 'uncertain')
        self.assertEqual(issues.decision(dict(RESULT, state='unknown'))['status'], 'uncertain')
        for field, value in [('urgency', float('nan')), ('urgency', 3), ('confidence', True), ('state', 'invented')]:
            with self.assertRaises(ValueError):
                issues.decision(dict(RESULT, **{field: value}))
        self.refresh(context=dict(CONTEXT, incomplete=True))
        self.assertEqual(next(iter(issues.load().values()))['status'], 'uncertain')

    def test_failure_is_recorded_without_detail_retried_and_cache_is_private(self):
        with patch.object(selection, 'registration', return_value=REGISTERED), \
             patch.object(github, 'issue_context', side_effect=github.GhError('private detail')):
            issues.refresh([('me', dict(ISSUE))], CFG, lambda _: None)
        row = next(iter(issues.load().values()))
        self.assertEqual(row['status'], 'error')
        self.assertNotIn('private detail', json.dumps(row))
        self.assertEqual(issues.rank(ISSUE, row)['source'], 'priority')
        self.assertEqual(self.refresh(), 1)
        self.assertEqual(issues.path().stat().st_mode & 0o777, 0o600)

    def test_per_tick_cap_takes_least_recently_checked_first(self):
        rows = [('me', dict(ISSUE, number=n)) for n in range(issues.PER_TICK + 2)]
        with patch.object(selection, 'registration', return_value=REGISTERED), \
             patch.object(github, 'issue_context', return_value=copy.deepcopy(CONTEXT)), \
             patch.object(insight_api, 'call', return_value=RESULT) as call:
            issues.refresh(rows, CFG, lambda _: None)
            self.assertEqual(call.call_count, issues.PER_TICK)
            issues.refresh(rows, CFG, lambda _: None)
            self.assertEqual(call.call_count, issues.PER_TICK + 2)

    def test_cleanup_includes_issue_cache(self):
        issues.save({'a': dict(ISSUE), 'b': dict(ISSUE, number=8)})
        cleanup.clean_pr('org/repo', 7)
        self.assertEqual(list(issues.load()), ['b'])


if __name__ == '__main__':
    unittest.main()

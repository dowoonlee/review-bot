import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from lib import drafts, github, store


class DraftReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name, value in [('STATE_DIR', str(self.root)), ('DRAFTS', str(self.root)), ('_ensure', lambda: None),
                            ('acquire_lock', lambda: True), ('release_lock', lambda: None)]:
            patch = mock.patch.object(store, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.cfg = {'repos': {'o/r': {'account': 'work'}}, 'account': 'personal'}

    def draft(self, number):
        data = {'repo': 'o/r', 'number': number, 'sha': 'abc', 'event': 'COMMENT'}
        path = Path(store.draft_payload_path('o/r', number))
        path.write_text(json.dumps(data))
        path.with_suffix('.md').write_text('historical report')
        return path

    def test_only_confirmed_terminal_prs_leave_pending_list(self):
        for number in range(1, 6):
            self.draft(number)
        with mock.patch.object(github, '_gh_json', side_effect=[
            {'state': 'MERGED'}, {'state': 'CLOSED'}, {'state': 'OPEN'},
            github.GhError('offline'), {'state': 'UNKNOWN'}]) as query:
            errors = drafts.reconcile(self.cfg)
        self.assertEqual([d['number'] for d in store.pending_drafts()], [3, 4, 5])
        self.assertEqual(len(list((self.root / 'archived').glob('*.json'))), 2)
        self.assertEqual(len(list(self.root.glob('*.md'))), 5)
        self.assertEqual(len(errors), 1)
        self.assertTrue(all(c.kwargs['account'] == 'work' for c in query.call_args_list))

    def test_busy_review_is_not_disturbed(self):
        self.draft(1)
        with mock.patch.object(store, 'acquire_lock', return_value=False), \
                mock.patch.object(github, '_gh_json') as query:
            drafts.reconcile(self.cfg)
        query.assert_not_called()
        self.assertEqual(len(store.pending_drafts()), 1)

    def test_replaced_draft_survives_old_query_result(self):
        path = self.draft(1)
        def changed(*args, **kwargs):
            d = json.loads(path.read_text());d['sha'] = 'new';path.write_text(json.dumps(d))
            return {'state': 'MERGED'}
        with mock.patch.object(github, '_gh_json', side_effect=changed):
            drafts.reconcile(self.cfg)
        self.assertEqual(store.pending_drafts()[0]['sha'], 'new')


class DraftEventTests(DraftReconciliationTests):
    def draft(self, number, event='COMMENT'):
        data = {'repo': 'o/r', 'number': number, 'sha': 'abc', 'event': event,
                'reason': 'judge said comment', 'at': '2026-10-16T09:00:00',
                'decision': {'judgeEvent': event, 'finalEvent': event,
                             'summary': 'judge said comment', 'reasons': []}}
        path = Path(store.draft_payload_path('o/r', number))
        path.write_text(json.dumps(data))
        path.with_suffix('.md').write_text('<!-- event: %s -->\nreport' % event)
        return path

    def test_comment_draft_can_become_approval_and_remembers_the_judge(self):
        path = self.draft(1)
        draft = drafts.set_event('o/r', 1, 'APPROVE')
        saved = json.loads(path.read_text())
        self.assertEqual(saved, draft)
        self.assertEqual(saved['event'], 'APPROVE')
        self.assertEqual(saved['at'], '2026-10-16T09:00:00')
        self.assertEqual(saved['override']['from'], 'COMMENT')
        self.assertEqual(saved['override']['decision']['finalEvent'], 'COMMENT')
        self.assertEqual(saved['decision']['judgeEvent'], 'COMMENT')
        self.assertEqual(saved['decision']['finalEvent'], 'APPROVE')
        self.assertEqual([r['code'] for r in saved['decision']['reasons']], ['HUMAN_OVERRIDE'])
        self.assertEqual(saved['decision']['reasons'][0]['source'], 'user')
        self.assertIn('승인', saved['reason'])
        self.assertTrue(path.with_suffix('.md').read_text().startswith('<!-- event: APPROVE -->\nreport'))

    def test_returning_to_the_judged_event_restores_the_original_decision(self):
        path = self.draft(1)
        original = json.loads(path.read_text())
        drafts.set_event('o/r', 1, 'APPROVE')
        drafts.set_event('o/r', 1, 'REQUEST_CHANGES')
        twice = json.loads(path.read_text())
        self.assertEqual(twice['override']['from'], 'COMMENT')
        self.assertEqual([r['code'] for r in twice['decision']['reasons']], ['HUMAN_OVERRIDE'])
        drafts.set_event('o/r', 1, 'COMMENT')
        self.assertEqual(json.loads(path.read_text()), original)
        self.assertTrue(path.with_suffix('.md').read_text().startswith('<!-- event: COMMENT -->'))

    def test_same_event_and_bad_input_leave_the_draft_alone(self):
        path = self.draft(1)
        before = path.read_text()
        drafts.set_event('o/r', 1, 'COMMENT')
        self.assertEqual(path.read_text(), before)
        with self.assertRaises(ValueError):
            drafts.set_event('o/r', 1, 'MERGE')
        self.assertEqual(path.read_text(), before)
        with self.assertRaises(FileNotFoundError):
            drafts.set_event('o/r', 2, 'APPROVE')

    def test_draft_without_decision_or_report_still_changes(self):
        path = Path(store.draft_payload_path('o/r', 3))
        path.write_text(json.dumps({'repo': 'o/r', 'number': 3, 'sha': 'abc'}))
        draft = drafts.set_event('o/r', 3, 'APPROVE')
        self.assertEqual(draft['event'], 'APPROVE')
        self.assertEqual(draft['override']['from'], 'COMMENT')
        self.assertEqual(draft['decision']['judgeEvent'], 'COMMENT')


if __name__ == '__main__':
    unittest.main()

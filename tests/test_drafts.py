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
        for name, value in [('DRAFTS', str(self.root)), ('_ensure', lambda: None),
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


if __name__ == '__main__':
    unittest.main()

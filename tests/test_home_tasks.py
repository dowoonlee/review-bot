import copy
import tempfile
import unittest
from unittest.mock import patch
from lib import home_tasks as tasks, store, github

BASE = {'state': 'OPEN', 'head': 'after-my-push', 'requestedAt': None, 'feedback': {'r1':'hash1'},
        'failures': {}, 'conflict': False, 'incomplete': False, 'updatedAt': 'now'}

class HomeTasksTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        patcher=patch.object(store,'STATE_DIR',temp.name);patcher.start();self.addCleanup(patcher.stop)
    def start(self,direction='outgoing',label='reply'):
        return tasks.start('org/repo',1,'me',direction,'title',label,'codex')
    def completed(self,direction='outgoing'):
        row=self.start(direction)
        with patch.object(tasks,'snapshot',return_value=copy.deepcopy(BASE)):
            return tasks.complete(row['id'])
    def test_link_session_persists_and_repeated_link_preserves_task(self):
        session = '12345678-1234-5678-9abc-123456789abc'
        row = tasks.start('org/repo', 1, 'me', 'outgoing', agent='codex', session_id='  ' + session.upper() + '  ')
        again = tasks.start('org/repo', 1, 'me', 'outgoing', agent='codex', session_id=session)
        self.assertEqual(row['id'], again['id'])
        self.assertEqual(tasks.load()['tasks'][row['id']]['sessionID'], session)
        with patch.object(tasks, 'snapshot', return_value=BASE):
            self.assertEqual(tasks.complete(row['id'])['status'], 'completed')

    def test_terminal_link_without_conversation_id_and_tab_replacement(self):
        terminal = '12345678-1234-5678-9abc-123456789abc'
        row = tasks.start('org/repo', 1, 'me', 'outgoing', agent='codex', terminal_id=terminal)
        again = tasks.start('org/repo', 1, 'me', 'outgoing', agent='codex', terminal_id=terminal)
        self.assertEqual(row['id'], again['id'])
        self.assertEqual(row['terminalID'], terminal)
        self.assertNotIn('sessionID', row)
        other = tasks.start('org/repo', 1, 'me', 'outgoing', agent='codex', terminal_id='22345678-1234-5678-9abc-123456789abc')
        self.assertNotEqual(row['id'], other['id'])
        with self.assertRaises(tasks.TaskError): tasks.complete(row['id'])
        with self.assertRaises(tasks.TaskError):
            tasks.start('org/repo', 1, 'me', 'outgoing', agent='codex', terminal_id='bad')
        self.assertEqual(tasks.load()['current'][other['requestID']], other['id'])

    def test_invalid_link_does_not_replace_current_task(self):
        row = self.start()
        for value in ['', 'not-a-session', '$(touch /tmp/bad)']:
            with self.assertRaises(tasks.TaskError):
                tasks.start('org/repo', 1, 'me', 'outgoing', agent='codex', session_id=value)
        with self.assertRaises(tasks.TaskError):
            tasks.start('org/repo', 1, 'me', 'outgoing', agent='manual', session_id='12345678-1234-5678-9abc-123456789abc')
        self.assertEqual(tasks.load()['current'][row['requestID']], row['id'])

    def test_link_new_session_rejects_previous_completion(self):
        old = self.start()
        linked = tasks.start('org/repo', 1, 'me', 'outgoing', agent='claude', session_id='12345678-1234-5678-9abc-123456789abc')
        with self.assertRaises(tasks.TaskError):
            tasks.complete(old['id'])
        self.assertEqual(tasks.load()['current'][old['requestID']], linked['id'])

    def test_session_exit_does_not_complete_and_completion_is_idempotent(self):
        row=self.start()
        self.assertEqual(tasks.change(row['id'],'stopped')['status'],'stopped')
        with patch.object(tasks,'snapshot',return_value=BASE) as snapshot:
            done=tasks.complete(row['id'])
            again=tasks.complete(row['id'])
            self.assertEqual(snapshot.call_count,1)
        self.assertEqual(done['baseline']['head'],'after-my-push')
        self.assertEqual(done,again)
        self.assertEqual(tasks.change(row['id'],'stopped')['status'],'completed')
        self.assertEqual(tasks.path().stat().st_mode & 0o777,0o600)
    def test_old_session_cannot_complete_new_session(self):
        old=self.start();new=self.start()
        with patch.object(tasks,'snapshot') as snapshot:
            with self.assertRaises(tasks.TaskError):tasks.complete(old['id'])
            snapshot.assert_not_called()
        self.assertEqual(tasks.load()['current'][new['requestID']],new['id'])
    def test_completion_does_not_overwrite_cancel_during_network_fetch(self):
        row=self.start()
        def snapshot(_):
            tasks.change(row['id'],'cancelled')
            return BASE
        with patch.object(tasks,'snapshot',side_effect=snapshot):
            with self.assertRaises(tasks.TaskError):tasks.complete(row['id'])
        self.assertEqual(tasks.load()['tasks'][row['id']]['status'],'cancelled')
    def test_failed_baseline_fetch_keeps_task_open(self):
        row=self.start()
        with patch.object(tasks,'snapshot',side_effect=github.GhError('private')):
            with self.assertRaises(tasks.TaskError):tasks.complete(row['id'])
        self.assertEqual(tasks.load()['tasks'][row['id']]['status'],'running')
    def test_only_new_actionable_events_reopen(self):
        done=self.completed()
        self.assertIsNone(tasks.reason_for_reopen(done,BASE))
        self.assertIsNone(tasks.reason_for_reopen(done,dict(BASE,head='another-own-push')))
        for change in [{'feedback': {'r1':'hash1','r2':'new'}}, {'feedback':{'r1':'edited'}},
                       {'failures':{'build':'run2'}}, {'requestedAt':'later'}, {'conflict':True}]:
            self.assertIsNotNone(tasks.reason_for_reopen(done,dict(BASE,**change)))
        self.assertIsNone(tasks.reason_for_reopen(done,dict(BASE,state='MERGED',feedback={'new':'x'})))
        incoming=self.completed('incoming')
        self.assertIsNotNone(tasks.reason_for_reopen(incoming,dict(BASE,head='new-head')))
    def test_approval_reopens_sent_pr_once(self):
        done=self.completed()
        self.assertEqual(tasks.reason_for_reopen(done,dict(BASE,approved=True)),tasks.APPROVAL_REASON)
        self.assertIsNone(tasks.reason_for_reopen(done,dict(BASE,approved=True,state='MERGED')))
        self.assertIsNone(tasks.reason_for_reopen(self.completed('incoming'),dict(BASE,approved=True)))
        row=self.start(label=tasks.MERGE_CHECK_LABEL)
        with patch.object(tasks,'snapshot',return_value=dict(BASE,approved=True,approvals=['a1'])):
            after=tasks.complete(row['id'])
        self.assertTrue(after['baseline']['approved'])
        self.assertIsNone(tasks.reason_for_reopen(after,dict(BASE,approved=True,approvals=['a1'])))
    def test_completing_other_step_while_approved_still_leads_to_merge_check(self):
        row=self.start(label='수정 요청 검토')
        with patch.object(tasks,'snapshot',return_value=dict(BASE,approved=True,approvals=['a1'])):
            done=tasks.complete(row['id'])
        self.assertFalse(done['baseline']['approved'])
        self.assertEqual(done['baseline']['approvals'],['a1'])
        with patch.object(tasks,'snapshot',return_value=dict(BASE,approved=True,approvals=['a1'])):
            tasks.reconcile({done['requestID']})
        row=tasks.load()['tasks'][done['id']]
        self.assertEqual((row['status'],row['reopenReason'],row.get('mergeCheck')),('reopened',tasks.APPROVAL_REASON,True))
        with patch.object(tasks,'snapshot',return_value=dict(BASE,approved=True,approvals=['a1'])):
            again=tasks.complete(row['id'])
        self.assertTrue(again['baseline']['approved'])
        self.assertIsNone(tasks.reason_for_reopen(again,dict(BASE,approved=True,approvals=['a1'])))
    def test_new_approval_after_merge_check_reopens_again(self):
        row=self.start(label=tasks.MERGE_CHECK_LABEL)
        with patch.object(tasks,'snapshot',return_value=dict(BASE,approved=True,approvals=['a1'])):
            done=tasks.complete(row['id'])
        self.assertEqual(tasks.reason_for_reopen(done,dict(BASE,approved=True,approvals=['a1','a2'])),tasks.APPROVAL_REASON)
        self.assertIsNone(tasks.reason_for_reopen(done,dict(BASE,approved=False,approvals=['a1','a2'])))
        legacy=dict(done,baseline={k:v for k,v in done['baseline'].items() if k!='approvals'})
        self.assertEqual(tasks.reason_for_reopen(legacy,dict(BASE,approved=True,approvals=['a1'])),tasks.APPROVAL_REASON)
    def test_approved_follows_review_decision_then_reviewers(self):
        self.assertTrue(tasks.approved({'reviewDecision':'APPROVED','reviewRequests':[{'login':'x'}]}))
        self.assertFalse(tasks.approved({'reviewDecision':'REVIEW_REQUIRED','latestReviews':[{'state':'APPROVED'}]}))
        self.assertTrue(tasks.approved({'reviewDecision':None,'latestReviews':[{'state':'APPROVED'}],'reviewRequests':[]}))
        self.assertFalse(tasks.approved({'reviewDecision':None,'latestReviews':[{'state':'APPROVED'}],'reviewRequests':[{'login':'x'}]}))
        self.assertFalse(tasks.approved({'reviewDecision':None,'latestReviews':[{'state':'APPROVED'},{'state':'COMMENTED'}]}))
        self.assertFalse(tasks.approved({'reviewDecision':None,'latestReviews':[]}))
    def test_reconcile_and_manual_reopen(self):
        done=self.completed()
        with patch.object(tasks,'snapshot',return_value=dict(BASE,feedback={'r2':'new'})):
            tasks.reconcile({done['requestID']})
        row=tasks.load()['tasks'][done['id']]
        self.assertEqual(row['status'],'reopened')
        with patch.object(tasks,'snapshot',return_value=BASE):tasks.complete(row['id'])
        self.assertEqual(tasks.change(row['id'],'reopened')['status'],'reopened')
    def test_snapshot_ignores_own_comments_and_resolved_threads(self):
        context={'head':'h','sources':[{'id':'own','author':'me','body':'done'},
            {'id':'resolved','author':'other','resolved':True},
            {'id':'approval','author':'other','state':'APPROVED'},
            {'id':'question','author':'other','body':'why?'}]}
        meta={'headRefOid':'h','state':'OPEN','statusCheckRollup':[]}
        with patch.object(github,'request_context',return_value=context),patch.object(github,'pr_meta',return_value=meta):
            snapshot=tasks.snapshot(self.start())
        self.assertEqual(set(snapshot['feedback']),{'question'})
        self.assertEqual(snapshot['approvals'],['approval'])
        self.assertFalse(snapshot['approved'])
        with patch.object(github,'request_context',return_value=context),patch.object(github,'pr_meta',return_value=dict(meta,reviewDecision='APPROVED')):
            self.assertTrue(tasks.snapshot(self.start())['approved'])
    def test_forget_preserves_other_accounts_and_prs(self):
        row=self.start();other=tasks.start('org/repo',2,'me','outgoing')
        tasks.forget('org/repo',1)
        self.assertNotIn(row['id'],tasks.load()['tasks'])
        self.assertIn(other['id'],tasks.load()['tasks'])

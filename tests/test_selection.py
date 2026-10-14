import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from lib import selection, insights, insight_api, github, store, cleanup

REGISTERED = {'enabled': True, 'model': 'jev-latest', 'registration': 'generation1', 'keyReady': True}
PR = {'repo': 'org/repo', 'number': 1, 'updatedAt': 'now'}
CFG = {'repos': {'org/repo': {}}, 'insights': {'enabled': False}}
RESULT = {'actor': 'self', 'action': 'reply', 'urgency': 1.4, 'confidence': 0.92, 'inputTokens': 123, 'outputTokens': 12}
CONTEXT = {'head': 'h1', 'sources': [{'id': 'r1', 'body': 'Why?', 'url': 'https://github.com/org/repo/pull/1'}], 'incomplete': False}

class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for name, value in {'STATE_DIR': self.temp.name, 'DRAFTS': self.temp.name+'/drafts', 'REVIEWED':self.temp.name+'/reviewed.json'}.items():
            p=patch.object(store,name,value);p.start();self.addCleanup(p.stop)

    def refresh(self, context=CONTEXT, registered=REGISTERED, result=RESULT):
        with patch.object(selection, 'registration', return_value=registered), \
             patch.object(github, 'request_context', return_value=copy.deepcopy(context)), \
             patch.object(insight_api, 'call', return_value=result) as call:
            selection.refresh([('me', PR, 'outgoing')], CFG, lambda _:None)
            return call.call_count

    def test_unregistered_does_not_touch_helper_or_keychain(self):
        with patch.object(selection.subprocess, 'run', return_value=subprocess.CompletedProcess([],1,'')), \
             patch.object(insight_api, 'call') as call:
            self.assertFalse(selection.registration()['enabled'])
            call.assert_not_called()
        self.assertEqual(self.refresh(registered={'enabled':False}),0)
        self.assertFalse(selection.path().exists())

    def test_changed_conversation_model_or_registration_invalidates_cache(self):
        self.assertEqual(self.refresh(),1)
        self.assertEqual(self.refresh(),0)
        changed=copy.deepcopy(CONTEXT);changed['sources'][0]['body']='Another question'
        self.assertEqual(self.refresh(changed),1)
        self.assertEqual(self.refresh(registered=dict(REGISTERED,registration='generation2')),1)
        self.assertEqual(self.refresh(registered=dict(REGISTERED,model='jev-next')),1)

    def test_disabled_or_reregistered_hides_previous_decisions(self):
        self.refresh()
        cache=selection.load()
        self.assertIsNone(selection.view(cache,{'enabled':False},'me',PR,'outgoing'))
        view=selection.view(cache,dict(REGISTERED,registration='new'),'me',PR,'outgoing')
        self.assertEqual(view['status'],'pending')
        self.assertEqual(selection.view(cache,REGISTERED,'me',dict(PR,updatedAt='later'),'outgoing')['status'],'stale')

    def test_uncertain_and_incomplete_never_ready(self):
        self.assertEqual(selection.decision(dict(RESULT,confidence=0.4))['status'],'uncertain')
        self.assertEqual(selection.decision(dict(RESULT,actor='unknown'))['status'],'uncertain')
        self.refresh(dict(CONTEXT,incomplete=True))
        self.assertEqual(next(iter(selection.load().values()))['status'],'uncertain')
        for field,value in [('urgency',float('nan')),('confidence',True),('actor','invented')]:
            with self.assertRaises(ValueError): selection.decision(dict(RESULT,**{field:value}))

    def test_failed_refresh_is_retried_and_does_not_erase_other_pr(self):
        self.refresh()
        with patch.object(selection,'registration',return_value=REGISTERED), \
             patch.object(github,'request_context',side_effect=github.GhError('private detail')):
            selection.refresh([('me',PR,'outgoing')],CFG,lambda _:None)
        row=next(iter(selection.load().values()))
        self.assertEqual(row['status'],'error')
        self.assertNotIn('private detail',json.dumps(row))
        self.assertEqual(self.refresh(),1)
        self.assertEqual(selection.path().stat().st_mode & 0o777,0o600)

    def test_cleanup_includes_selection_cache(self):
        selection.save({'a':dict(PR),'b':dict(PR,number=2)})
        cleanup.clean_pr('org/repo',1)
        self.assertEqual(list(selection.load()),['b'])

import copy
import unittest

from lib import checkpoints, config


class CacheLayerTests(unittest.TestCase):
    def setUp(self):
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.meta = {'_repo': 'o/r', 'headRefOid': 'head', 'baseRefOid': 'base'}
        self.original = checkpoints.identity(self.meta, self.cfg, 'me')

    def test_delivery_formatting_and_notification_changes_keep_evidence(self):
        self.cfg['post'].update(dryRun=False, polish={'model': 'other'}, note='new note')
        self.cfg['notify']['enabled'] = False
        self.cfg['limits']['maxPerTick'] = 9
        self.assertEqual(checkpoints.identity(self.meta, self.cfg, 'me'), self.original)

    def test_runtime_and_review_settings_invalidate_only_their_layer_and_decision(self):
        for part in ('runtime', 'review', 'decision'):
            cfg = copy.deepcopy(self.cfg)
            if part == 'runtime': cfg['review']['runtime']['image'] = 'other-image'
            elif part == 'review': cfg['review']['model'] = 'other-model'
            else: cfg['post']['judgeModel'] = 'other-model'
            value = checkpoints.identity(self.meta, cfg, 'me')
            with self.subTest(part=part):
                self.assertNotEqual(value['decision'], self.original['decision'])
                self.assertEqual(value['runtime'] == self.original['runtime'], part != 'runtime')
                self.assertEqual(value['review'] == self.original['review'], part != 'review')

    def test_judgment_changes_when_ci_or_execution_evidence_changes(self):
        key = checkpoints.judgment_key(self.original, {'runtime': {'status': 'complete'}}, [], {'state': 'pending'})
        changed = checkpoints.judgment_key(self.original, {'runtime': {'status': 'complete'}}, [], {'state': 'success'})
        self.assertNotEqual(key, changed)
        changed = checkpoints.judgment_key(self.original, {'runtime': {'status': 'incomplete'}}, [], {'state': 'pending'})
        self.assertNotEqual(key, changed)

    def test_cached_finding_ids_survive_json_roundtrip_without_rebilling(self):
        verdict = checkpoints.saved_judgment({'judgment': {'key': 'k', 'verdict': {
            'event': 'APPROVE', 'cost_usd': 5, 'placements': {'0': 'inline'},
            'dispositions': {'0': {'disposition': 'optional', 'dispositionReason': 'optional'}}}}}, 'k')
        self.assertEqual(verdict['placements'][0], 'inline')
        self.assertEqual(verdict['dispositions'][0]['disposition'], 'optional')
        self.assertEqual(verdict['cost_usd'], 0)

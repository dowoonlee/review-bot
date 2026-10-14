import copy
import io
import json
import runpy
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from lib import insights, insight_api, github, store, cleanup

PR = {'repo': 'org/repo', 'number': 17, 'updatedAt': 'now'}
CFG = {'insights': {'enabled': True}, 'repos': {'org/repo': {'enabled': True}}}
CONTEXT = {'head': 'head1', 'incomplete': False, 'sources': [
    {'id': 'r1', 'body': 'Why?', 'url': 'https://github.com/org/repo/pull/17#discussion_r1'}]}
RESULT = {'summary': '질문에 답변하세요', 'items': [
    {'kind': 'question', 'text': '이유 설명', 'sourceIds': ['r1'], 'replyDraft': '이유를 확인하겠습니다.'}]}


def envelope(result=RESULT, stop='end_turn'):
    return io.StringIO(json.dumps({'stop_reason': stop, 'content': [{'type': 'text', 'text': json.dumps(result)}],
                                 'usage': {'input_tokens': 12, 'output_tokens': 20}}))


class InsightsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = patch.object(store, 'STATE_DIR', self.temp.name)
        self.state.start()
        for name in ("REVIEWED", "DRAFTS"):
            patcher = patch.object(store, name, str(Path(self.temp.name) / name.lower()))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.state.stop)

    def refresh(self, context=CONTEXT, targets=None, cfg=CFG):
        with patch.object(insight_api, 'key_state', return_value='ok'), \
             patch.object(github, 'request_context', return_value=context), \
             patch.object(insight_api, 'call', side_effect=lambda _: json.load(envelope())) as request:
            insights.refresh(targets or [(None, PR, 'outgoing')], cfg, lambda _: None)
            return request.call_count

    def test_unchanged_reuses_changed_reply_reanalyzes(self):
        self.assertEqual(self.refresh(), 1)
        self.assertEqual(self.refresh(), 0)
        changed = copy.deepcopy(CONTEXT)
        changed['sources'][0]['body'] = 'Edited question'
        self.assertEqual(self.refresh(changed), 1)
        changed['head'] = 'head2'
        self.assertEqual(self.refresh(changed), 1)

    def test_resolution_and_model_invalidate_cache(self):
        self.refresh()
        changed = copy.deepcopy(CONTEXT)
        changed['sources'][0]['resolved'] = True
        self.assertEqual(self.refresh(changed), 1)
        self.assertEqual(insights.load()[insights.key(None, PR, 'outgoing')]['items'], [])
        cfg = copy.deepcopy(CFG)
        cfg['insights']['model'] = 'claude-other'
        self.assertEqual(self.refresh(changed, cfg=cfg), 1)

    def test_disabled_and_missing_key_do_not_call_api(self):
        with patch.object(insight_api, 'key_state', return_value='missing'), \
             patch.object(github, 'request_context') as fetch:
            insights.refresh([(None, PR, 'outgoing')], CFG, lambda _: None)
            fetch.assert_not_called()
        self.assertEqual(insights.load()[insights.key(None, PR, 'outgoing')]['status'], 'missing_key')
        self.assertEqual(self.refresh(cfg={'insights': {'enabled': False}}), 0)

    def test_failure_preserves_previous_analysis_and_retry_is_not_cached_as_success(self):
        self.refresh()
        changed = copy.deepcopy(CONTEXT)
        changed['head'] = 'changed'
        with patch.object(insight_api, 'key_state', return_value='ok'), \
             patch.object(github, 'request_context', return_value=changed), \
             patch.object(insights, 'analyze', side_effect=insights.InsightError('Claude API HTTP 429')):
            insights.refresh([(None, PR, 'outgoing')], CFG, lambda _: None)
        row = insights.load()[insights.key(None, PR, 'outgoing')]
        self.assertEqual(row['status'], 'error')
        self.assertEqual(row['summary'], RESULT['summary'])
        self.assertIn('429', row['error'])
        self.assertEqual(self.refresh(changed), 1)

    def test_cap_rotates_and_accounts_are_separate(self):
        targets = [('a', PR, 'outgoing'), ('b', PR, 'outgoing')]
        cfg = copy.deepcopy(CFG)
        cfg['insights']['maxPerTick'] = 1
        self.assertEqual(self.refresh(targets=targets, cfg=cfg), 1)
        self.assertEqual(self.refresh(targets=targets, cfg=cfg), 1)
        self.assertEqual(len(insights.load()), 2)

    def test_stale_and_disabled_are_not_current_advice(self):
        self.refresh()
        current = insights.view(insights.load(), CFG, None, PR, 'outgoing')
        self.assertIn('답변 검토 1건', current['nextAction'])
        stale = insights.view(insights.load(), CFG, None, dict(PR, updatedAt='later'), 'outgoing')
        self.assertEqual(stale['status'], 'stale')
        self.assertNotIn('답변 검토 1건', stale['nextAction'])
        disabled = insights.view(insights.load(), {}, None, PR, 'outgoing')
        self.assertEqual(disabled['status'], 'disabled')
        self.assertNotIn('summary', disabled)

    def test_recovered_keychain_is_pending_not_an_active_failure(self):
        key = insights.key(None, PR, 'outgoing')
        row = {'status': 'error', 'error': '키체인 접근을 확인하지 못했습니다.', 'summary': 'previous'}
        cache = {key: row}
        current = insights.view(cache, CFG, None, PR, 'outgoing', credential_state='ok')
        self.assertEqual(current['status'], 'pending')
        self.assertIn('복구', current['error'])
        self.assertEqual(cache[key]['status'], 'error')  # status reads do not mutate cache
        blocked = insights.view(cache, CFG, None, PR, 'outgoing', credential_state='locked')
        self.assertEqual(blocked['status'], 'error')
        cache[key] = {'status': 'error', 'error': 'Claude 분석 형식 오류', 'errorKind': 'analysis'}
        self.assertEqual(insights.view(cache, CFG, None, PR, 'outgoing', credential_state='ok')['status'], 'error')

    def test_fact_priority_overrides_llm_action(self):
        self.refresh()
        row = insights.view(insights.load(), CFG, None, dict(PR, checksState='FAILURE'), 'outgoing')
        self.assertEqual(row['nextAction'], 'CI 실패 확인')

    def test_no_api_key_persisted_and_files_private(self):
        self.refresh()
        text = insights._path().read_text()
        self.assertNotIn('fake-secret', text)
        self.assertEqual(insights._path().stat().st_mode & 0o777, 0o600)

    def test_rejects_unknown_evidence_and_truncated_output(self):
        bad = copy.deepcopy(RESULT)
        bad['items'][0]['sourceIds'] = ['invented']
        for response in [envelope(bad), envelope(stop='max_tokens')]:
            with patch.object(insight_api, 'call', return_value=json.load(response)):
                with self.assertRaises(insights.InsightError):
                    insights.analyze(CONTEXT, insights.DEFAULTS)

    def test_truncated_response_retries_once_with_larger_budget_and_counts_usage(self):
        truncated = {'stop_reason': 'max_tokens', 'content': [{'type': 'text', 'text': '{"summary":'}],
                     'usage': {'input_tokens': 100, 'output_tokens': 1800}}
        with patch.object(insight_api, 'call', side_effect=[truncated, json.load(envelope())]) as call:
            result = insights.analyze(CONTEXT, insights.DEFAULTS)
        self.assertEqual(call.call_count, 2)
        self.assertEqual(call.call_args_list[1].args[0]['maxTokens'], 16384)
        self.assertEqual(result['inputTokens'], 112)
        self.assertEqual(result['outputTokens'], 1820)
        self.assertEqual(result['apiCalls'], 2)

    def test_truncation_budget_exhaustion_has_specific_error_and_no_loop(self):
        truncated = {'stop_reason': 'max_tokens', 'usage': {'output_tokens': 16384}}
        with patch.object(insight_api, 'call', return_value=truncated) as call:
            with self.assertRaisesRegex(insights.InsightError, '출력 토큰 한도'):
                insights.analyze(CONTEXT, dict(insights.DEFAULTS, maxTokens=16384))
        self.assertEqual(call.call_count, 1)

    def test_workspace_forwarded_and_unknown_settings_not_exposed(self):
        cfg = {'insights': {'apiKey': 'do-not-output', 'token': 'do-not-output', 'workspaceID': 'wrkspc_test'}}
        options = insights.settings(cfg)
        self.assertNotIn('apiKey', options)
        self.assertNotIn('token', options)
        with patch.object(insight_api, 'call', return_value=json.load(envelope())) as call:
            insights.analyze(CONTEXT, options)
        self.assertEqual(call.call_args.args[0]['workspaceID'], 'wrkspc_test')
        self.assertNotIn('do-not-output', json.dumps(call.call_args.args[0]))
        self.assertFalse(insights.valid_workspace('wrkspc_test\r\nx-api-key: bad'))
        self.assertEqual(insights.settings({'insights': {'workspaceID': 'sk-ant-secret'}})['workspaceID'], '')

    def test_cleanup_removes_only_target_pr_insights(self):
        data = {'a': dict(PR, checkedAt=10), 'b': dict(PR, number=18)}
        insights.save(data)
        cleanup.clean_pr('org/repo', 17)
        self.assertEqual(insights.load(), {'b': data['b']})

    def test_options_are_bounded(self):
        cfg = {'insights': {'timeoutSec': 900, 'maxPerTick': 1000, 'maxTokens': None}}
        self.assertEqual(insights.settings(cfg)['timeoutSec'], 180)
        self.assertEqual(insights.settings(cfg)['maxPerTick'], 20)
        self.assertEqual(insights.settings(cfg)['maxTokens'], 8192)

    def test_context_keeps_resolution_and_marks_partial_data(self):
        pr = {'title': 'Title', 'body': 'x' * 5000, 'url': 'https://github.com/org/repo/pull/17',
              'headRefOid': 'head', 'baseRefOid': 'base', 'isDraft': False,
              'reviewThreads': {'pageInfo': {'hasPreviousPage': True}, 'nodes': [
                  {'id': 't1', 'isResolved': True, 'comments': {'nodes': [
                      {'id': 'c1', 'url': 'https://github.com/org/repo/pull/17#discussion_r1', 'body': 'done'}]}}]}}
        with patch.object(github, '_gh_json', return_value={'data': {'repository': {'pullRequest': pr}}}) as query:
            context = github.request_context('org/repo', 17, account='work')
        self.assertTrue(context['incomplete'])
        self.assertTrue(context['sources'][1]['resolved'])
        self.assertEqual(len(context['sources'][0]['body']), 4000)
        self.assertEqual(query.call_args.kwargs['account'], 'work')

    def test_tick_analysis_failure_does_not_abort_existing_review_pipeline(self):
        bot = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'review-bot'))
        cfg = {'repos': {}, 'limits': {}, 'notify': {'enabled': False}}
        with patch.object(bot['drafts_mod'], 'reconcile', return_value=[]), \
             patch.object(bot['store'], 'load', return_value={}), \
             patch.object(bot['checkpoints'], 'watching', return_value=[]), \
             patch.object(insights, 'refresh', side_effect=OSError('disk')), \
             patch.dict(bot['_tick'].__globals__, {'_gc': lambda cfg: None, '_log': lambda msg: None}):
            self.assertEqual(bot['_tick'](cfg), 0)


class InsightBridgeTests(unittest.TestCase):
    def test_bridge_passes_only_payload_and_no_key(self):
        import subprocess
        with patch.object(insight_api.os.path, 'isfile', return_value=True), \
             patch.object(insight_api.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '{"ok":true}')) as run:
            self.assertTrue(insight_api.call({'action': 'check', 'workspaceID': 'wrkspc_test'})['ok'])
        self.assertEqual(run.call_args.args[0][-1], '--insight-api')
        self.assertNotIn('env', run.call_args.kwargs)
        self.assertNotIn('pass_fds', run.call_args.kwargs)

    def test_helper_errors_are_not_echoed(self):
        import subprocess
        for output in ['not-json-secret', '{"error":"secret","detail":"secret"}']:
            with patch.object(insight_api.os.path, 'isfile', return_value=True), \
                 patch.object(insight_api.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, output)):
                with self.assertRaises(insight_api.APIError) as error:
                    insight_api.call({'action': 'check'})
                self.assertNotIn('secret', str(error.exception))


class KeychainAccessTests(unittest.TestCase):
    def test_locked_is_preserved_and_error_does_not_expose_provider_detail(self):
        import subprocess
        with patch.object(insight_api.os.path, 'isfile', return_value=True), \
             patch.object(insight_api.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '{"keyState":"locked"}')):
            self.assertEqual(insight_api.key_state(), 'locked')
        with patch.object(insight_api.os.path, 'isfile', return_value=True), \
             patch.object(insight_api.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '{"error":"keychain_locked","detail":"private-data"}')):
            with self.assertRaises(insight_api.APIError) as error:
                insight_api.call({'action': 'check'})
            self.assertIn('키체인 접근 확인', str(error.exception))
            self.assertNotIn('private-data', str(error.exception))

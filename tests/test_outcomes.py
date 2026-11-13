"""해결률 — 봇이 남긴 인라인 지적이 머지 전에 어떻게 됐는지 스레드 기록으로 분류한다."""
import json
from pathlib import Path
import runpy
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from lib import github, outcomes, reviewer, store

BOT = '<!-- review-bot -->\n**[P2]** 승인 카드에 링크 대상이 안 보임 · 확인 필요\n\n본문'


def thread(body=BOT, outdated=False, resolved=False, replies=(), path='a.ts', line=10):
    nodes = [{'author': {'login': 'bot'}, 'body': body, 'createdAt': '2026-11-10T01:00:00Z',
              'url': 'u', 'originalCommit': {'oid': 'abcdef1234567890'}}]
    nodes += [{'author': {'login': who}, 'body': text} for who, text in replies]
    return {'isOutdated': outdated, 'isResolved': resolved, 'path': path, 'line': None if outdated else line,
            'originalLine': line, 'comments': {'totalCount': len(nodes), 'nodes': nodes}}


class OutcomeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for name, value in (('STATE_DIR', root), ('REVIEWED', root / 'reviewed.json')):
            patch = mock.patch.object(store, name, str(value)); patch.start(); self.addCleanup(patch.stop)
        patch = mock.patch.object(outcomes, 'path', lambda: str(root / 'outcomes.json'))
        patch.start(); self.addCleanup(patch.stop)

    def test_only_the_bots_own_comments_are_findings_and_their_heading_is_read(self):
        self.assertEqual(outcomes.parse_comment(BOT), ('P2', '승인 카드에 링크 대상이 안 보임', '확인 필요'))
        self.assertEqual(outcomes.parse_comment('<!-- review-bot -->\n**[P1]** 옛 형식 제목\n\nb'), ('P1', '옛 형식 제목', None))
        self.assertEqual(outcomes.parse_comment('<!-- review-bot -->\n_설계 대안 제시이며…_\n**[altitude]** a · b · 선택적 개선'),
                         ('altitude', 'a · b', '선택적 개선'))
        self.assertIsNone(outcomes.parse_comment('**[P2]** 사람이 쓴 코멘트 · 확인 필요'))
        rows = outcomes.findings([thread(replies=[('author', '반영했습니다'), ('bot', '<!-- review-bot --> 봇의 답')]),
                                  thread(body='사람의 리뷰'), {'comments': {'nodes': []}}])
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['priority'], rows[0]['line'], rows[0]['commit'], rows[0]['codeChanged']),
                         ('P2', 10, 'abcdef123456', False))
        self.assertEqual(rows[0]['replies'], [{'by': 'author', 'body': '반영했습니다'}])

    def test_without_the_model_only_what_the_flags_prove_is_claimed(self):
        row = lambda **k: outcomes.findings([thread(**k)])[0]
        self.assertEqual(outcomes.mechanical(row(outdated=True)), 'fixed')
        self.assertEqual(outcomes.mechanical(row()), 'unanswered')
        # Closing a thread is not fixing it, and an answer has to be read to know what it says.
        self.assertEqual(outcomes.mechanical(row(resolved=True)), 'unanswered')
        self.assertEqual(outcomes.mechanical(row(replies=[('author', '의도된 동작입니다')])), 'unknown')

    def test_the_model_reads_the_replies_and_a_bad_answer_falls_back_to_the_flags(self):
        rows = outcomes.findings([thread(replies=[('author', '반영했습니다 (c5c594d)')]),
                                  thread(replies=[('author', '의도된 동작입니다')]), thread(outdated=True), thread()])
        answer = {'outcomes': [{'id': 0, 'outcome': 'fixed', 'why': '답글이 고쳤다고 함'},
                               {'id': 1, 'outcome': 'declined', 'why': '의도'},
                               {'id': 2, 'outcome': 'approved'}], '_meta': {'cost_usd': 0.02}}
        with mock.patch.object(reviewer, '_run', return_value=answer) as run:
            judged, cost = outcomes.classify('o/r', 1, 'MERGED', rows)
        prompt = run.call_args.args[0]
        self.assertIn('반영했습니다 (c5c594d)', prompt)
        self.assertNotIn('<!-- review-bot -->', prompt)
        self.assertEqual(run.call_args.kwargs['tools'], '')     # 판정자는 아무 도구도 쓰지 않는다
        self.assertEqual([(r['outcome'], r['judged']) for r in judged],
                         [('fixed', 'model'), ('declined', 'model'), ('fixed', 'flags'), ('unanswered', 'flags')])
        self.assertEqual(cost, 0.02)
        with mock.patch.object(reviewer, '_run', side_effect=reviewer.ReviewError('boom', cost=0.01)):
            judged, cost = outcomes.classify('o/r', 1, 'MERGED', outcomes.findings([thread(replies=[('a', 'x')])]))
        self.assertEqual((judged[0]['outcome'], cost), ('unknown', 0.01))

    def collect(self, state, threads):
        return mock.patch.object(github, 'review_threads', return_value={
            'state': state, 'mergedAt': '2026-11-12T00:00:00Z' if state == 'MERGED' else None,
            'closedAt': '2026-11-12T00:00:00Z', 'head': 'h', 'threads': threads})

    def test_a_merged_pr_is_collected_once_and_an_open_one_is_asked_again_later(self):
        store.save({'o/r#1': {'posted': True, 'sha': 'a'}, 'o/r#2': {'posted': True, 'sha': 'b'},
                    'o/r#3': {'posted': False, 'sha': 'c'}, 'o/r#4': {'posted': True, 'discarded': True}})
        cfg = {'repos': {'o/r': {}}}
        judge = mock.Mock(return_value={'outcomes': [{'id': 0, 'outcome': 'fixed', 'why': 'w'}], '_meta': {}})
        states = {1: ('MERGED', [thread(outdated=True)]), 2: ('OPEN', [thread()])}

        def threads(repo, number, account=None, timeout=60):
            state, nodes = states[number]
            return {'state': state, 'mergedAt': 'x' if state == 'MERGED' else None, 'closedAt': None, 'threads': nodes}
        with mock.patch.object(github, 'review_threads', side_effect=threads) as asked, \
                mock.patch.object(reviewer, '_run', judge), \
                mock.patch.object(store, 'read_verify', return_value=[
                    {'repo': 'o/r', 'number': 1, 'path': 'a.ts', 'title': '승인 카드에 링크 대상이 안 보임',
                     'fate': 'inline', 'agent': 'security-reviewer', 'confidence': 82}]):
            first = outcomes.refresh(cfg)
            self.assertEqual((first['collected'], first['open']), (1, 1))
            self.assertEqual(sorted(c.args[1] for c in asked.call_args_list), [1, 2])   # 게시한 것만 묻는다
            again = outcomes.refresh(cfg)                       # 끝난 PR 도, 방금 본 열린 PR 도 다시 묻지 않는다
            self.assertEqual((again['collected'], again['open'], asked.call_count), (0, 0, 2))
            states[2] = ('MERGED', [])
            later = outcomes.refresh(cfg, recheck_hours=0)
            self.assertEqual((later['collected'], asked.call_count), (1, 3))
        saved = outcomes.load()
        row = saved['o/r#1']['findings'][0]
        self.assertEqual((row['outcome'], row['agent'], row['confidence'], row['replies']), ('fixed', 'security-reviewer', 82, 0))
        self.assertNotIn('body', row)
        self.assertEqual(saved['o/r#2']['findings'], [])
        judge.assert_called_once()                              # 지적이 없는 PR 에는 모델을 부르지 않는다

    def test_failures_and_the_limit_leave_the_rest_for_next_time(self):
        store.save({'o/r#%d' % n: {'posted': True} for n in (1, 2, 3)})
        with mock.patch.object(github, 'review_threads', side_effect=github.GhError('down')) as asked:
            result = outcomes.refresh({'repos': {'o/r': {}}}, limit=2)
        self.assertEqual((result['collected'], asked.call_count, outcomes.load()), (0, 2, {}))
        self.assertEqual(outcomes.refresh({'outcomes': {'enabled': False}})['collected'], 0)

    def test_rate_counts_merged_prs_only_and_breaks_down_by_what_might_explain_it(self):
        row = lambda outcome, **extra: dict({'outcome': outcome, 'priority': 'P2', 'action': '확인 필요',
                                             'postedAt': '2026-11-10T01:00:00Z', 'codeChanged': outcome == 'fixed'}, **extra)
        saved = {'a#1': {'state': 'MERGED', 'closedAt': '2026-11-12T00:00:00Z', 'findings': [
                     row('fixed', agent='security-reviewer', confidence=82), row('fixed', priority='P1', confidence=60),
                     row('declined', agent='code-reviewer', confidence=55), row('unanswered', postedAt='2026-10-01T00:00:00Z')]},
                 'a#2': {'state': 'CLOSED', 'closedAt': '2026-11-12T00:00:00Z', 'findings': [row('unanswered')]},
                 'a#3': {'state': 'OPEN', 'checked': 1}}
        summary = outcomes.summarize(saved)
        self.assertEqual((summary['mergedPRs'], summary['findings'], summary['fixed'], summary['rate']), (1, 4, 2, 0.5))
        self.assertEqual(summary['outcomes'], {'fixed': 2, 'declined': 1, 'deferred': 0, 'unanswered': 1, 'unknown': 0})
        self.assertEqual((summary['open'], summary['closedUnmerged'], summary['codeChanged']), (1, 1, 2))
        self.assertEqual(summary['byPriority'], [{'name': 'P1', 'fixed': 1, 'total': 1}, {'name': 'P2', 'fixed': 1, 'total': 3}])
        self.assertIn({'name': 'security-reviewer', 'fixed': 1, 'total': 1}, summary['byAgent'])
        self.assertIn({'name': '51-75', 'fixed': 1, 'total': 2}, summary['byConfidence'])
        self.assertEqual([g['name'] for g in summary['byMonth']], ['2026-10', '2026-11'])
        self.assertEqual(outcomes.summarize(saved, since_days=0.0001)['findings'], 0)
        self.assertIsNone(outcomes.summarize({})['rate'])

    def test_command_prints_the_rate_and_the_findings_that_were_not_fixed(self):
        Path(outcomes.path()).write_text(json.dumps({'o/r#1': {
            'repo': 'o/r', 'number': 1, 'state': 'MERGED', 'closedAt': '2026-11-12T00:00:00Z', 'findings': [
                {'outcome': 'fixed', 'priority': 'P2', 'title': 'a', 'postedAt': '2026-11-10'},
                {'outcome': 'declined', 'priority': 'P3', 'title': '의도된 것', 'why': '작성자가 의도라고 답함', 'postedAt': '2026-11-10'}]}}))
        bot = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'review-bot'))
        with mock.patch('builtins.print') as printed:
            self.assertEqual(bot['cmd_outcomes'](SimpleNamespace(refresh=False, json=False, since=None, list=True), {}), 0)
        text = '\n'.join(str(c.args[0]) for c in printed.call_args_list)
        self.assertIn('해결률 50% (1/2)', text)
        self.assertIn('r#1 [P3] 의도된 것 — 거절·의도된 동작 · 작성자가 의도라고 답함', text)


if __name__ == '__main__':
    unittest.main()

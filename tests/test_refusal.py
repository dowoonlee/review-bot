"""안전 분류기가 요청을 막으면 같은 모델로 되풀이하지 않고 모델을 한 번 바꾼다."""
import json
import unittest
from unittest import mock

from lib import reviewer


def envelope(stop_reason='end_turn', cost=0.5, result='{"findings": []}'):
    return json.dumps({'type': 'result', 'stop_reason': stop_reason, 'total_cost_usd': cost,
                       'result': result, 'session_id': 's'})


REFUSED = (envelope('refusal', 0.25, "API Error: Sonnet 5.5's safeguards flagged this message"), '', 1)


class RefusalTest(unittest.TestCase):
    def setUp(self):
        reviewer._REFUSED.clear()
        self.addCleanup(reviewer._REFUSED.clear)

    def test_a_task_refused_once_skips_that_model_when_called_again(self):
        self.run_with([REFUSED, (envelope(), '', 0)])
        payload, models = self.run_with([(envelope(cost=0.4), '', 0)])
        self.assertEqual(models, ['opus'])
        self.assertEqual(payload['_meta']['refusedModel'], 'sonnet')
        self.assertEqual(payload['_meta']['cost_usd'], 0.4)

    def test_the_memory_is_per_worktree_and_task(self):
        self.run_with([REFUSED, (envelope(), '', 0)])
        with mock.patch.object(reviewer, '_invoke', return_value=(envelope(), '', 0)) as invoke:
            reviewer._run('p', '/other', 'sonnet', 0, 60, 'batch-003-code-reviewer', tools='')
            reviewer._run('p', '/tmp', 'sonnet', 0, 60, 'batch-004-code-reviewer', tools='')
        self.assertEqual([c.args[0][c.args[0].index('--model') + 1] for c in invoke.call_args_list],
                         ['sonnet', 'sonnet'])

    def run_with(self, outputs, model='sonnet'):
        models = []

        def invoke(cmd, prompt, cwd, timeout, env_extra=None):
            models.append(cmd[cmd.index('--model') + 1])
            return outputs[len(models) - 1]
        with mock.patch.object(reviewer, '_invoke', side_effect=invoke):
            try:
                return reviewer._run('p', '/tmp', model, 0, 60, 'batch-003-code-reviewer', tools=''), models
            except reviewer.ReviewError as exc:
                return exc, models

    def test_refused_request_runs_once_more_on_the_other_model(self):
        payload, models = self.run_with([REFUSED, (envelope(), '', 0)])
        self.assertEqual(models, ['sonnet', 'opus'])
        self.assertEqual(payload['findings'], [])
        self.assertEqual(payload['_meta']['refusedModel'], 'sonnet')
        self.assertEqual(payload['_meta']['cost_usd'], 0.75)

    def test_refusal_on_both_models_fails_without_another_round(self):
        error, models = self.run_with([REFUSED, REFUSED])
        self.assertEqual(models, ['sonnet', 'opus'])
        self.assertTrue(error.refused)
        self.assertEqual(error.cost, 0.5)

    def test_the_fallback_model_itself_is_not_run_twice(self):
        error, models = self.run_with([REFUSED], model='opus')
        self.assertEqual(models, ['opus'])
        self.assertTrue(error.refused)

    def test_other_failures_keep_the_model(self):
        error, models = self.run_with([(envelope('end_turn', 0.1, 'not json'), 'boom', 1)])
        self.assertEqual(models, ['sonnet'])
        self.assertFalse(error.refused)

    def test_verdict_does_not_repeat_a_refused_request(self):
        refused = reviewer.ReviewError('막힘', cost=1, refused=True)
        with mock.patch.object(reviewer, '_run', side_effect=refused) as run:
            with self.assertRaises(reviewer.ReviewError) as caught:
                reviewer.judge({'_repo': 'o/r', '_number': 1, 'title': 't'}, {}, [], ['COMMENT'])
        self.assertEqual(run.call_count, 1)
        self.assertTrue(caught.exception.refused)


if __name__ == '__main__':
    unittest.main()

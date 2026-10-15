import runpy
import unittest
from pathlib import Path
from unittest.mock import patch

from lib import github


def pr(number=1, requested=True):
    return {
        "number": number, "title": "Fix", "url": "https://github.com/org/repo/pull/%s" % number,
        "repository": {"nameWithOwner": "org/repo"}, "author": {"login": "me"},
        "updatedAt": "2026-09-14T00:00:00Z", "isDraft": False,
        "reviewDecision": "APPROVED", "mergeable": "MERGEABLE",
        "reviewRequests": {"nodes": [], "pageInfo": {"hasNextPage": False}},
        "latestReviews": {"nodes": [{"id": "R1", "author": {"login": "reviewer"}, "state": "APPROVED"}],
                          "pageInfo": {"hasNextPage": False}},
        "timelineItems": {"nodes": [{"createdAt": "2026-09-13T00:00:00Z"}] if requested else []},
        "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "FAILURE"}}}]},
    }


def page(nodes, cursor=None):
    return {"data": {"search": {"nodes": nodes, "issueCount": len(nodes),
            "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor}}}}


class SentRequestsTests(unittest.TestCase):
    @patch.object(github, "whoami", return_value="me")
    def test_completed_requests_remain_but_unsolicited_reviews_do_not_qualify(self, _):
        with patch.object(github, "_gh_json", return_value=page([pr(), pr(2, False)])):
            rows, warnings = github.sent_review_requests("me")
        self.assertEqual([r["number"] for r in rows], [1])
        self.assertEqual(rows[0]["reviewDecision"], "APPROVED")
        self.assertEqual(rows[0]["checksState"], "FAILURE")
        self.assertFalse(warnings)

    @patch.object(github, "whoami", return_value="me")
    def test_rerequest_overrides_previous_approval_and_includes_teams(self, _):
        node = pr(requested=False)
        node["reviewRequests"]["nodes"] = [
            {"requestedReviewer": {"login": "reviewer"}},
            {"requestedReviewer": {"slug": "team", "organization": {"login": "org"}}},
            {"requestedReviewer": None},
        ]
        with patch.object(github, "_gh_json", return_value=page([node])):
            rows, _ = github.sent_review_requests()
        self.assertEqual({r["name"]: r["state"] for r in rows[0]["reviewers"]},
                         {"reviewer": "PENDING", "org/team": "PENDING"})
        self.assertIsNone(rows[0]["requestedAt"])
        self.assertEqual(rows[0]["responses"][0]["id"], "R1")
        self.assertEqual(rows[0]["responses"][0]["state"], "APPROVED")

    @patch.object(github, "whoami", return_value="me")
    def test_pagination_preserves_account_and_cursor(self, _):
        with patch.object(github, "_gh_json", side_effect=[page([pr()], "next"), page([pr(2)])]) as query:
            rows, _ = github.sent_review_requests("work")
        self.assertEqual(len(rows), 2)
        self.assertIn("cursor=next", query.call_args.args[0])
        self.assertEqual(query.call_args.kwargs["account"], "work")
        self.assertIn("searchQuery=is:pr is:open author:me sort:updated-desc", query.call_args.args[0])

    @patch.object(github, "whoami", return_value="me")
    def test_graphql_partial_error_fails_instead_of_clearing_previous_results(self, _):
        response = page([pr()])
        response["errors"] = [{"message": "unavailable"}]
        with patch.object(github, "_gh_json", return_value=response):
            with self.assertRaises(github.GhError):
                github.sent_review_requests()

    @patch.object(github, "whoami", return_value="me")
    def test_reviewer_truncation_is_reported_and_missing_checks_are_unknown(self, _):
        node = pr()
        node["latestReviews"]["pageInfo"]["hasNextPage"] = True
        node["commits"] = {"nodes": []}
        with patch.object(github, "_gh_json", return_value=page([node])):
            rows, warnings = github.sent_review_requests()
        self.assertEqual(len(warnings), 1)
        self.assertIsNone(rows[0]["checksState"])

    def test_status_explains_why_a_received_pr_is_held(self):
        bot = runpy.run_path(str(Path(__file__).resolve().parents[1] / "review-bot"))
        status, hold = bot["status_payload"], bot["hold_reason"]
        cfg = {"repos": {"org/a": {"account": "a"}}, "post": {}, "limits": {}, "review": {}}
        draft = {"repo": "org/a", "number": 1, "author": "them", "isDraft": True}
        replacements = {"_config_info": lambda _: {}, "_paused_info": lambda: None,
                        "_lock_info": lambda: None}
        with patch.dict(status.__globals__, replacements), \
             patch.object(github, "whoami", side_effect=lambda account: account), \
             patch.object(github, "review_requests", return_value=[draft]), \
             patch.object(github, "sent_review_requests", return_value=([], [])), \
             patch.object(github, "assigned_issues", return_value=([], [])), \
             patch.object(bot["store"], "load", return_value={}), \
             patch.object(bot["store"], "pending_drafts", return_value=[]):
            result = status(cfg, skip_details=True)
        self.assertEqual(result["pending"][0]["status"], "new")
        self.assertIn("draft", result["pending"][0]["holdReason"])
        # skipDrafts 를 끄면 보류 사유가 없다. 본인 PR 은 draft 여부와 무관하게 보류.
        self.assertIsNone(hold({"review": {"skipDrafts": False}}, draft))
        self.assertIn("본인", hold(cfg, dict(draft, author="Me"), me="me"))
        self.assertIsNone(hold(cfg, dict(draft, isDraft=False)))

    def test_status_isolates_sent_failure_from_received_and_other_accounts(self):
        bot = runpy.run_path(str(Path(__file__).resolve().parents[1] / "review-bot"))
        status = bot["status_payload"]
        cfg = {"repos": {"org/a": {"account": "a"}, "org/b": {"account": "b"}},
               "post": {}, "limits": {}}
        sent = {"account": "b", "repo": "org/b", "number": 2}
        received = {"repo": "org/a", "number": 1}
        replacements = {"_config_info": lambda _: {}, "_paused_info": lambda: None,
                        "_lock_info": lambda: None}
        with patch.dict(status.__globals__, replacements), \
             patch.object(github, "whoami", side_effect=lambda account: account), \
             patch.object(github, "review_requests", side_effect=[[received], []]), \
             patch.object(github, "sent_review_requests", side_effect=[github.GhError("offline"), ([sent], [])]), \
             patch.object(github, "assigned_issues", return_value=([], [])), \
             patch.object(bot["store"], "load", return_value={}), \
             patch.object(bot["store"], "pending_drafts", return_value=[]):
            result = status(cfg, skip_details=True)
        self.assertEqual(result["pending"][0]["number"], 1)
        self.assertEqual(result["outgoing"], [sent])
        self.assertEqual(result["outgoingFailedAccounts"], ["a"])
        self.assertEqual(len(result["outgoingErrors"]), 1)
        self.assertFalse(result["errors"])


if __name__ == "__main__":
    unittest.main()

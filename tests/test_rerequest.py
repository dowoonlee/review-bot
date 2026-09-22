"""같은 커밋 그대로 온 리뷰 재요청을 봇이 보는가.

리뷰를 게시하면 GitHub 이 리뷰 요청을 걷어 간다. 그 뒤 다시 요청이 들어왔다는 것은
사람이 한 번 더 봐 달라고 말한 것인데, 커밋 지문만 보던 봇은 매 틱 "이미 리뷰함" 으로
건너뛰었다 — skcc-ai/c2#6116 이 그렇게 갇혔다.
"""
import unittest
from unittest import mock

from lib import github, store


class AlreadyDoneTests(unittest.TestCase):
    def reviewed(self, **fields):
        state = {}
        store.mark(state, "o/r", 1, "sha1", posted=True, **fields)
        return state

    def done(self, state, requested_at=None, sha="sha1"):
        return store.already_done(state, "o/r", 1, sha, True, False,
                                  requested_at=requested_at)

    def test_same_request_stays_skipped(self):
        state = self.reviewed(requestedAt="2026-09-21T09:35:34Z")
        self.assertTrue(self.done(state, "2026-09-21T09:35:34Z"))

    def test_newer_request_on_same_commit_is_reviewed_again(self):
        state = self.reviewed(requestedAt="2026-09-21T09:35:34Z")
        self.assertFalse(self.done(state, "2026-09-22T01:40:09Z"))

    def test_record_without_answered_request_is_reviewed_again(self):
        """이 고침 전에 남은 기록. 요청이 와 있으면 한 번은 다시 본다."""
        state = self.reviewed()
        self.assertFalse(self.done(state, "2026-09-22T01:40:09Z"))

    def test_unknown_request_time_falls_back_to_commit_only(self):
        """조회가 죽었을 때 돈이 드는 쪽으로 기울지 않는다."""
        state = self.reviewed(requestedAt="2026-09-21T09:35:34Z")
        self.assertTrue(self.done(state, None))

    def test_answering_a_request_settles_it(self):
        state = self.reviewed(requestedAt="2026-09-21T09:35:34Z")
        self.assertFalse(self.done(state, "2026-09-22T01:40:09Z"))
        store.mark(state, "o/r", 1, "sha1", posted=True,
                   requestedAt="2026-09-22T01:40:09Z")
        self.assertTrue(self.done(state, "2026-09-22T01:40:09Z"))

    def test_unobserved_request_never_overwrites_the_answered_one(self):
        """조회 실패로 `None` 이 덮이면 다음 틱이 재요청으로 오인해 돈을 또 쓴다."""
        state = self.reviewed(requestedAt="2026-09-22T01:40:09Z")
        store.mark(state, "o/r", 1, "sha2", posted=True, requestedAt=None)
        self.assertEqual(state[store.key("o/r", 1)]["requestedAt"],
                         "2026-09-22T01:40:09Z")

    def test_new_commit_still_wins(self):
        state = self.reviewed(requestedAt="2026-09-21T09:35:34Z")
        self.assertFalse(self.done(state, "2026-09-21T09:35:34Z", sha="sha2"))


class LastReviewRequestTests(unittest.TestCase):
    EVENTS = [
        {"createdAt": "2026-09-17T10:47:04Z", "actor": {"login": "author"},
         "requestedReviewer": {"login": "someone-else"}},
        {"createdAt": "2026-09-21T09:35:34Z", "actor": {"login": "author"},
         "requestedReviewer": {"login": "Me"}},
        {"createdAt": "2026-09-22T01:40:09Z", "actor": {"login": "author"},
         "requestedReviewer": {"login": "me"}},
    ]

    def payload(self, nodes):
        return {"data": {"repository": {"pullRequest": {
            "timelineItems": {"nodes": nodes}}}}}

    def test_takes_the_last_request_aimed_at_me(self):
        with mock.patch.object(github, "_gh_json",
                               return_value=self.payload(self.EVENTS)):
            self.assertEqual(github.last_review_request("o/r", 1, "me"),
                             {"at": "2026-09-22T01:40:09Z", "by": "author"})

    def test_requests_for_other_reviewers_are_not_mine(self):
        with mock.patch.object(github, "_gh_json",
                               return_value=self.payload(self.EVENTS[:1])):
            self.assertIsNone(github.last_review_request("o/r", 1, "me"))

    def test_no_history_is_not_a_request(self):
        with mock.patch.object(github, "_gh_json",
                               return_value=self.payload([])):
            self.assertIsNone(github.last_review_request("o/r", 1, "me"))

    def test_graphql_errors_are_not_read_as_no_request(self):
        """빈 결과로 읽으면 재요청이 조용히 사라진다."""
        with mock.patch.object(github, "_gh_json",
                               return_value={"errors": [{"message": "boom"}]}):
            with self.assertRaises(github.GhError):
                github.last_review_request("o/r", 1, "me")

    def test_requester_name_still_comes_from_the_same_history(self):
        with mock.patch.object(github, "_gh_json",
                               return_value=self.payload(self.EVENTS)):
            self.assertEqual(github.review_requester("o/r", 1, "me"), "author")

    def test_requester_survives_a_failed_lookup(self):
        with mock.patch.object(github, "_gh_json",
                               side_effect=github.GhError("down")):
            self.assertIsNone(github.review_requester("o/r", 1, "me"))


if __name__ == "__main__":
    unittest.main()

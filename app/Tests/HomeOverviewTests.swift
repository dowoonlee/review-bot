import XCTest
@testable import ReviewBotApp

final class HomeOverviewTests: XCTestCase {
    func snapshot(_ outgoing: [[String: Any]]) throws -> StatusSnapshot {
        let rows = outgoing.map { fields in
            ["repo": "org/repo", "number": 1, "title": "Review", "url": "https://github.com/org/repo/pull/1",
             "author": "me", "account": "me", "updatedAt": "now", "isDraft": false, "reviewers": []]
                .merging(fields) { _, new in new }
        }
        let data = try JSONSerialization.data(withJSONObject: ["at": "now", "accounts": [], "repos": [],
            "dryRun": true, "maxPerTick": 2, "pending": [], "outgoing": rows, "reviewed": [:], "errors": []])
        return try JSONDecoder().decode(StatusSnapshot.self, from: data)
    }
    func insight(_ state: String) -> [String: Any] {
        ["nextAction": "reply", "status": state, "summary": "A question", "items": [
            ["kind": "question", "text": "Explain retry", "sources": [], "replyDraft": ""]]]
    }
    func testOnlyCurrentAIContributesActions() throws {
        let ready = HomeOverview(try snapshot([["insight": insight("ready")]]))
        XCTAssertEqual(ready.actions.first?.action, "질문 답변")
        for state in ["stale", "error", "disabled"] {
            let old = HomeOverview(try snapshot([["insight": insight(state)]]))
            XCTAssertTrue(old.actions.isEmpty)
            XCTAssertEqual(old.waiting.count, 1)
        }
    }
    func testCIAndConflictOutrankAIAndApprovalDoesNotMeanMergeable() throws {
        let value = HomeOverview(try snapshot([
            ["number": 1, "reviewDecision": "APPROVED", "checksState": "PENDING"],
            ["number": 2, "checksState": "FAILURE", "insight": insight("ready")],
            ["number": 3, "reviewDecision": "APPROVED", "checksState": "SUCCESS"]]))
        XCTAssertEqual(value.waiting.count, 1)
        XCTAssertEqual(value.actions.first?.action, "CI 실패 확인")
        XCTAssertEqual(value.actions.last?.action, "최종 머지 조건 확인")
    }
    func testChangesRemainUntilExplicitAcknowledgementAndDisappearanceIsNotMerge() throws {
        let old = HomeOverview(try snapshot([["checksState": "PENDING"]]))
        let new = HomeOverview(try snapshot([["checksState": "FAILURE"]]))
        XCTAssertTrue(new.changes(since: nil).isEmpty)
        XCTAssertEqual(new.changes(since: old.stamps).first?.text, "CI 실패 발생")
        XCTAssertEqual(new.changes(since: old.stamps).count, 1)
        XCTAssertTrue(new.changes(since: new.stamps).isEmpty)
        XCTAssertTrue(HomeOverview(try snapshot([])).changes(since: old.stamps).isEmpty)
    }
    func testRepeatedReviewDetectedWithoutTimestampChange() throws {
        let old = HomeOverview(try snapshot([["responses": [["id": "r1"]]]]))
        let new = HomeOverview(try snapshot([["responses": [["id": "r2"]]]]))
        XCTAssertEqual(new.changes(since: old.stamps).count, 1)
    }
    func testAccountIdentityAndDuplicateRows() throws {
        let overview = HomeOverview(try snapshot([["account": "a"], ["account": "b"], ["account": "a"]]))
        XCTAssertEqual(overview.requests.count, 2)
        XCTAssertEqual(overview.stamps.count, 2)
    }
}

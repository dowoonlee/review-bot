import XCTest
@testable import ReviewBotApp

final class DraftEventTests: XCTestCase {
    func testCommentDraftOffersApproval() throws {
        let plain = try decode(["repo": "o/r", "number": 1])
        XCTAssertEqual(plain.effectiveEvent, "COMMENT")
        XCTAssertTrue(plain.canPromoteToApprove)
        XCTAssertNil(plain.revertEvent)

        let comment = try decode(["repo": "o/r", "number": 1, "event": "COMMENT"])
        XCTAssertTrue(comment.canPromoteToApprove)

        for event in ["APPROVE", "REQUEST_CHANGES"] {
            let d = try decode(["repo": "o/r", "number": 1, "event": event])
            XCTAssertFalse(d.canPromoteToApprove, event)
            XCTAssertNil(d.revertEvent, event)
        }
    }

    func testOverriddenDraftOffersReturnToJudgedEvent() throws {
        let promoted = try decode(["repo": "o/r", "number": 1, "event": "APPROVE",
                                   "override": ["from": "COMMENT", "at": "2026-10-16T09:00:00"]])
        XCTAssertFalse(promoted.canPromoteToApprove)
        XCTAssertEqual(promoted.revertEvent, "COMMENT")
        XCTAssertEqual(promoted.overridden?.from, "COMMENT")

        let backAlready = try decode(["repo": "o/r", "number": 1, "event": "COMMENT",
                                      "override": ["from": "COMMENT"]])
        XCTAssertNil(backAlready.revertEvent)
        XCTAssertTrue(backAlready.canPromoteToApprove)
    }

    private func decode(_ object: [String: Any]) throws -> DraftPayload {
        try JSONDecoder().decode(DraftPayload.self, from: JSONSerialization.data(withJSONObject: object))
    }
}

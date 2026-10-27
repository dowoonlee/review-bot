import XCTest
@testable import ReviewBotApp

final class DraftEventTests: XCTestCase {
    func testDraftOffersEveryOtherEvent() throws {
        let plain = try decode(["repo": "o/r", "number": 1])
        XCTAssertEqual(plain.effectiveEvent, "COMMENT")
        XCTAssertEqual(plain.switchableEvents, ["APPROVE", "REQUEST_CHANGES"])
        XCTAssertNil(plain.revertEvent)

        let comment = try decode(["repo": "o/r", "number": 1, "event": "COMMENT"])
        XCTAssertEqual(comment.switchableEvents, ["APPROVE", "REQUEST_CHANGES"])

        let approve = try decode(["repo": "o/r", "number": 1, "event": "APPROVE"])
        XCTAssertEqual(approve.switchableEvents, ["COMMENT", "REQUEST_CHANGES"])
        XCTAssertNil(approve.revertEvent)

        let changes = try decode(["repo": "o/r", "number": 1, "event": "REQUEST_CHANGES"])
        XCTAssertEqual(changes.switchableEvents, ["APPROVE", "COMMENT"])
        XCTAssertNil(changes.revertEvent)
    }

    func testOverriddenDraftOffersReturnToJudgedEvent() throws {
        let promoted = try decode(["repo": "o/r", "number": 1, "event": "APPROVE",
                                   "override": ["from": "COMMENT", "at": "2026-10-16T09:00:00"]])
        XCTAssertEqual(promoted.switchableEvents, ["COMMENT", "REQUEST_CHANGES"])
        XCTAssertEqual(promoted.revertEvent, "COMMENT")
        XCTAssertEqual(promoted.overridden?.from, "COMMENT")

        let backAlready = try decode(["repo": "o/r", "number": 1, "event": "COMMENT",
                                      "override": ["from": "COMMENT"]])
        XCTAssertNil(backAlready.revertEvent)
        XCTAssertEqual(backAlready.switchableEvents, ["APPROVE", "REQUEST_CHANGES"])
    }

    func testDraftCarriesRequesterAndAuthor() throws {
        let draft = try decode(["repo": "o/r", "number": 1, "requestedBy": "alice", "author": "bob"])
        XCTAssertEqual(draft.requestedBy, "alice")
        XCTAssertEqual(draft.author, "bob")
        // 이 필드가 생기기 전의 초안도 그대로 열린다.
        let legacy = try decode(["repo": "o/r", "number": 1])
        XCTAssertNil(legacy.requestedBy)
        XCTAssertNil(legacy.author)
    }

    func testEventLabelsMatchTheBot() {
        XCTAssertEqual(eventLabel("APPROVE"), "승인")
        XCTAssertEqual(eventLabel("COMMENT"), "코멘트")
        XCTAssertEqual(eventLabel("REQUEST_CHANGES"), "변경 요청")
        XCTAssertEqual(eventLabel("PENDING"), "PENDING")
    }

    private func decode(_ object: [String: Any]) throws -> DraftPayload {
        try JSONDecoder().decode(DraftPayload.self, from: JSONSerialization.data(withJSONObject: object))
    }
}

import XCTest
@testable import ReviewBotApp

final class DraftGateTests: XCTestCase {
    private func current(_ repo: String, _ number: Int) throws -> CurrentRun {
        try JSONDecoder().decode(CurrentRun.self, from: Data(#"{"repo":"\#(repo)","number":\#(number)}"#.utf8))
    }

    func testIdleBotLeavesDraftOpen() throws {
        XCTAssertFalse(DraftGate.blocked(busy: false, running: false, current: nil, repo: "o/r", number: 1))
        // 봇이 멈춘 뒤 남은 current.json 은 잠금 근거가 아니다.
        XCTAssertFalse(DraftGate.blocked(busy: false, running: false, current: try current("o/r", 1), repo: "o/r", number: 1))
    }

    func testOnlyTheReviewedPRIsLocked() throws {
        let reviewing = try current("o/r", 7597)
        XCTAssertTrue(DraftGate.blocked(busy: false, running: true, current: reviewing, repo: "o/r", number: 7597))
        XCTAssertFalse(DraftGate.blocked(busy: false, running: true, current: reviewing, repo: "o/r", number: 8132))
        XCTAssertFalse(DraftGate.blocked(busy: false, running: true, current: reviewing, repo: "other/r", number: 7597))
        // 어느 PR 을 보는지 모르면(current 없음) 다른 PR 의 초안은 막지 않는다 — CLI 가 PR lock 으로 최종 판단한다.
        XCTAssertFalse(DraftGate.blocked(busy: false, running: true, current: nil, repo: "o/r", number: 8132))
    }

    func testAppCommandInFlightLocksEveryDraft() throws {
        XCTAssertTrue(DraftGate.blocked(busy: true, running: false, current: nil, repo: "o/r", number: 1))
    }
}

final class QueuedReviewTests: XCTestCase {
    func testPositionCountsFromOneAndMissesWhenAbsent() throws {
        let queue = try JSONDecoder().decode([QueuedReview].self, from: Data(
            #"[{"repo":"o/r","number":7,"enqueuedAt":"2026-11-13T14:00:00","options":{"rejudge":true}},{"repo":"o/r","number":9}]"#.utf8))
        XCTAssertEqual(QueuedReview.position(in: queue, repo: "o/r", number: 7), 1)
        XCTAssertEqual(QueuedReview.position(in: queue, repo: "o/r", number: 9), 2)
        XCTAssertNil(QueuedReview.position(in: queue, repo: "o/r", number: 1))
        XCTAssertNil(QueuedReview.position(in: queue, repo: "x/y", number: 7))
    }
}

final class QueueRowTests: XCTestCase {
    private func pending(_ number: Int, status: String = "new", watched: Bool = true, hold: String? = nil) throws -> PendingPR {
        let holdJSON = hold.map { "\"\($0)\"" } ?? "null"
        return try JSONDecoder().decode(PendingPR.self, from: Data("""
        {"repo":"o/r","number":\(number),"title":"t\(number)","url":"u","author":"a","updatedAt":"x",
         "watched":\(watched),"status":"\(status)","holdReason":\(holdJSON)}
        """.utf8))
    }

    func testRunningThenRequestedInOrderThenWhatTheTickWillPick() throws {
        let current = try JSONDecoder().decode(CurrentRun.self, from: Data(#"{"repo":"o/r","number":1,"phase":"반증"}"#.utf8))
        XCTAssertEqual(current.phase, "반증")
        let queue = [QueuedReview(repo: "o/r", number: 5, enqueuedAt: nil), QueuedReview(repo: "o/r", number: 3, enqueuedAt: nil)]
        let rows = QueueRow.build(
            current: current, running: true, queue: queue,
            pending: [try pending(1), try pending(3), try pending(7), try pending(8, status: "draft"),
                      try pending(9, watched: false), try pending(10, hold: "draft")],
            titles: ["o/r#5": "from draft"])
        XCTAssertEqual(rows.map(\.number), [1, 5, 3, 7])
        XCTAssertEqual(rows.map(\.kind), [.running, .requested(position: 1), .requested(position: 2), .automatic])
        XCTAssertEqual(rows.map(\.title), [nil, "from draft", nil, "t7"])
    }

    func testIdleBotShowsOnlyWhatIsQueuedAndAStaleCurrentFileIsIgnored() throws {
        let stale = try JSONDecoder().decode(CurrentRun.self, from: Data(#"{"repo":"o/r","number":1}"#.utf8))
        let rows = QueueRow.build(current: stale, running: false,
                                  queue: [QueuedReview(repo: "o/r", number: 1, enqueuedAt: nil)], pending: [])
        XCTAssertEqual(rows.map(\.kind), [.requested(position: 1)])
        XCTAssertFalse(ReviewQueueSection.visible(running: false, queue: []))
        XCTAssertTrue(ReviewQueueSection.visible(running: true, queue: []))
    }
}

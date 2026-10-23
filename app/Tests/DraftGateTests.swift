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

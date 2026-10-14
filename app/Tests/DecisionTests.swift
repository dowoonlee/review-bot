import XCTest
@testable import ReviewBotApp

final class DecisionTests: XCTestCase {
    func testOldRecordsRemainReadableAndNewRecordsExplainDowngrade() throws {
        let old = try JSONDecoder().decode(RunRecord.self, from: Data(#"{"event":"COMMENT","reason":"old"}"#.utf8))
        XCTAssertNil(old.decision)
        let data = Data(#"{"event":"COMMENT","decision":{"judgeEvent":"APPROVE","finalEvent":"COMMENT","summary":"CI failed","reasons":[{"code":"CI_FAILED","message":"CI failed","source":"ci"}]}}"#.utf8)
        let current = try JSONDecoder().decode(RunRecord.self, from: data)
        XCTAssertEqual(current.decision?.judgeEvent, "APPROVE")
        XCTAssertEqual(current.decision?.finalEvent, "COMMENT")
        XCTAssertEqual(current.decision?.reasons?.first?.label, "CI 실패")
    }
}

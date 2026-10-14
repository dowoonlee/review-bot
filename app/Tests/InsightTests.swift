import XCTest
@testable import ReviewBotApp

final class InsightTests: XCTestCase {
    func testLegacyPRWithoutInsightAndCurrentPRWithEvidenceDecode() throws {
        let old = #"{"repo":"org/repo","number":1,"title":"Fix","url":"https://github.com/org/repo/pull/1","author":"a","updatedAt":"now","watched":true,"status":"new"}"#
        let pr = try JSONDecoder().decode(PendingPR.self, from: Data(old.utf8))
        XCTAssertNil(pr.insight)
        let data = #"{"nextAction":"답변 검토 1건","status":"ready","summary":"질문 확인","items":[{"kind":"question","text":"이유 설명","sources":[{"id":"R1","url":"https://github.com/org/repo/pull/1#discussion_r1"}],"replyDraft":"확인하겠습니다"}],"inputTokens":12,"outputTokens":20}"#
        let insight = try JSONDecoder().decode(RequestInsight.self, from: Data(data.utf8))
        XCTAssertEqual(insight.items?.first?.label, "답변 검토")
        XCTAssertEqual(insight.items?.first?.sources.first?.id, "R1")
        XCTAssertEqual(insight.inputTokens, 12)
    }

    func testMissingKeyStateDoesNotNeedPreviousAnalysis() throws {
        let data = #"{"status":"missing_key","nextAction":"리뷰 진행","error":"키를 등록하세요"}"#
        let insight = try JSONDecoder().decode(RequestInsight.self, from: Data(data.utf8))
        XCTAssertNil(insight.summary)
        XCTAssertEqual(insight.status, "missing_key")
    }
}

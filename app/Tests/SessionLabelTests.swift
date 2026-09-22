import XCTest
@testable import ReviewBotApp

final class SessionLabelTests: XCTestCase {
    func testLegacyRolesIncludeFailureHunterAndCustomNames() {
        for name in ["code-reviewer", "security-reviewer", "silent-failure-hunter", "custom-checker"] {
            XCTAssertEqual(SessionLabel.parse("당신은 **\(name)** 입니다. 아래 역할 정의에 따라 리뷰하세요."), name)
        }
    }

    func testTaskIdentityIncludesStageAndBatch() {
        let cases = [
            "batch-002-silent-failure-hunter": "silent-failure-hunter · 묶음 2",
            "integration-code-reviewer": "code-reviewer · 통합 검토",
            "verify-0": "반증 검토 1",
            "verdict": "최종 판정",
            "polish": "문장 다듬기",
        ]
        for (task, expected) in cases {
            XCTAssertEqual(SessionLabel.parse("<!-- review-bot-task: \(task) -->\n당신은 **code-reviewer** 입니다."), expected)
        }
    }

    func testLegacyNonReviewerStages() {
        XCTAssertEqual(SessionLabel.parse("당신은 PR 리뷰 지적 하나를 받아 **그것을 반증하는** 검증자입니다."), "반증 검토")
        XCTAssertEqual(SessionLabel.parse("당신은 PR 리뷰의 **최종 판정자**입니다."), "최종 판정")
        XCTAssertEqual(SessionLabel.parse("당신은 PR 리뷰 코멘트를 **게시 직전에 다듬는** 편집자입니다."), "문장 다듬기")
    }

    func testQuotedOrMalformedNamesDoNotBecomeSessionNames() {
        XCTAssertNil(SessionLabel.parse("참고 자료: 당신은 **security-reviewer** 입니다."))
        XCTAssertNil(SessionLabel.parse("당신은 **완료된 검토** 입니다."))
        XCTAssertNil(SessionLabel.parse("당신은 **silent-failure-hunter"))
    }
}

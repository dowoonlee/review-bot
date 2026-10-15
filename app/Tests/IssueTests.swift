import XCTest
@testable import ReviewBotApp

final class IssueTests: XCTestCase {
    private func issue(_ fields: [String: Any]) -> [String: Any] {
        ["repo": "org/repo", "number": 1, "title": "Issue", "url": "https://github.com/org/repo/issues/1",
         "author": "them", "account": "me", "createdAt": "2026-10-01T00:00:00Z", "updatedAt": "2026-10-02T00:00:00Z",
         "labels": ["project:CO"]].merging(fields) { _, new in new }
    }
    private func snapshot(_ issues: [[String: Any]]) throws -> StatusSnapshot {
        let data = try JSONSerialization.data(withJSONObject: ["at": "now", "accounts": [], "repos": [],
            "dryRun": true, "maxPerTick": 2, "pending": [], "issues": issues, "reviewed": [:], "errors": []])
        return try JSONDecoder().decode(StatusSnapshot.self, from: data)
    }

    func testJevScoreOutranksPriorityFieldAndTiesFallToDueDateThenRecency() throws {
        let rows: [[String: Any]] = [
            issue(["number": 1, "priority": "Urgent", "rank": ["score": 2.0, "source": "priority"]]),
            issue(["number": 2, "priority": "Low", "rank": ["score": 2.0, "source": "jev"],
                   "jev": ["status": "ready", "state": "actionable", "urgency": 2.0, "confidence": 0.95], "targetDate": "2026-10-20"]),
            issue(["number": 3, "priority": "Medium", "rank": ["score": 1.0, "source": "priority"], "updatedAt": "2026-10-05T00:00:00Z"]),
            issue(["number": 4, "priority": "Medium", "rank": ["score": 1.0, "source": "priority"], "updatedAt": "2026-10-01T00:00:00Z"]),
        ]
        let sorted = try snapshot(rows).issues!.sorted(by: AssignedIssue.urgentFirst)
        // 2 와 1 은 같은 점수 — 목표일이 있는 2 가 먼저. 3 과 4 는 같은 점수·마감 없음 — 최근 갱신 3 이 먼저.
        XCTAssertEqual(sorted.map(\.number), [2, 1, 3, 4])
        XCTAssertEqual(sorted[0].urgencyLabel, "긴급")
        XCTAssertEqual(sorted[2].urgencyLabel, "보통")
        XCTAssertEqual(sorted[0].jev?.stateLabel, "바로 착수 가능")
    }

    func testMissingRankIsNormalAndLowConfidenceJevIsNotUsable() throws {
        let rows: [[String: Any]] = [
            issue(["number": 1]),
            issue(["number": 2, "jev": ["status": "ready", "state": "blocked", "urgency": 1.9, "confidence": 0.5],
                   "rank": ["score": 0.5, "source": "priority"], "priority": "Low"]),
        ]
        let issues = try snapshot(rows).issues!
        XCTAssertEqual(issues[0].score, 1.0)
        XCTAssertEqual(issues[0].priorityLabel, "우선순위 미지정")
        XCTAssertFalse(issues[1].jev!.usable)
        XCTAssertNil(issues[1].jev!.stateLabel)
        XCTAssertEqual(issues[1].jev!.statusLabel, "Jev 확신 부족")
        XCTAssertEqual(issues[1].urgencyLabel, "낮음")
    }

    func testIssueAnswersAreWhitelistedAndInvalidChoicesRejected() throws {
        let data = Data(#"{"answers":{"state":{"type":"choice","choice":"blocked","confidence":0.9},"urgency":{"type":"score","score":1.5,"confidence":0.85}},"secret":"dummy-secret","usage":{"input_tokens":50,"output_tokens":5}}"#.utf8)
        let result = try JevAPI.sanitizedIssue(data)
        XCTAssertEqual(result["state"] as? String, "blocked")
        XCTAssertEqual(result["urgency"] as? Double, 1.5)
        XCTAssertEqual(result["confidence"] as? Double, 0.85)
        XCTAssertFalse(String(decoding: try JSONSerialization.data(withJSONObject: result), as: UTF8.self).contains("dummy-secret"))
        let bad = String(decoding: data, as: UTF8.self).replacingOccurrences(of: "\"blocked\"", with: "\"invented\"")
        XCTAssertThrowsError(try JevAPI.sanitizedIssue(Data(bad.utf8)))
        // PR 대화용 답(actor/action)은 이슈 검증을 통과하지 못한다 — 질문 세트가 섞이면 바로 드러나야 한다.
        let prShaped = Data(#"{"answers":{"actor":{"type":"choice","choice":"self","confidence":0.9},"action":{"type":"choice","choice":"fix","confidence":0.9},"urgency":{"type":"score","score":1,"confidence":0.9}}}"#.utf8)
        XCTAssertThrowsError(try JevAPI.sanitizedIssue(prShaped))
    }

    func testIssueRequestCarriesIssueQuestionsOnly() throws {
        let request = try JevAPI.request(model: "jev-latest", context: "issue context", token: "dummy-private-key", questions: JevAPI.issueQuestions)
        let body = try JSONSerialization.jsonObject(with: request.httpBody!) as! [String: Any]
        let questions = body["questions"] as! [String: Any]
        XCTAssertEqual(Set(questions.keys), ["urgency", "state"])
        XCTAssertEqual(request.url?.absoluteString, "https://api.typesafe.ai/v1/systemone")
    }

    func testIssuesPaneIsRestoredLikeOtherFixedPanes() {
        XCTAssertEqual(Selection.issues.stored, "issues")
        XCTAssertEqual(Selection.restore("issues"), .issues)
    }
}

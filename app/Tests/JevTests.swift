import XCTest
@testable import ReviewBotApp

final class JevTests: XCTestCase {
    func testRegistrationAutomaticallyGatesFeatureAndProviderKeysAreSeparate() {
        XCTAssertFalse(JevRegistration.isEnabled(model: "", generation: "g", keyReady: true))
        XCTAssertFalse(JevRegistration.isEnabled(model: "jev-latest", generation: "g", keyReady: false))
        XCTAssertTrue(JevRegistration.isEnabled(model: "jev-latest", generation: "g", keyReady: true))
        XCTAssertNotEqual(InsightKeychain.accessKey(for: JevRegistration.service), InsightKeychain.accessKey(for: InsightKeychain.service))
    }
    func testOnlyFixedTypeSafeDestinationReceivesBearerKey() throws {
        let request = try JevAPI.request(model: "jev-latest", context: "review context", token: "dummy-private-key")
        XCTAssertEqual(request.url?.absoluteString, "https://api.typesafe.ai/v1/systemone")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer dummy-private-key")
        XCTAssertNil(request.value(forHTTPHeaderField: "x-api-key"))
        XCTAssertThrowsError(try JevAPI.request(model: "jev-latest", context: "", token: "key\r\nHeader:bad"))
        XCTAssertFalse(String(decoding: request.httpBody!, as: UTF8.self).contains("dummy-private-key"))
        XCTAssertThrowsError(try JevAPI.request(model: "jev-test\r\nHeader", context: "", token: "dummy"))
    }
    func testResponseWhitelistDropsSecretsAndRejectsInvalidClassification() throws {
        let data = Data(#"{"answers":{"actor":{"type":"choice","choice":"self","confidence":0.95},"action":{"type":"choice","choice":"reply","confidence":0.9},"urgency":{"type":"score","score":1.7,"confidence":0.85}},"secret":"dummy-secret","usage":{"input_tokens":120}}"#.utf8)
        let result = try JevAPI.sanitized(data)
        XCTAssertEqual(result["confidence"] as? Double, 0.85)
        XCTAssertFalse(String(decoding: try JSONSerialization.data(withJSONObject: result), as: UTF8.self).contains("dummy-secret"))
        let bad = String(decoding: data, as: UTF8.self).replacingOccurrences(of: "\"self\"", with: "\"secret-choice\"")
        XCTAssertThrowsError(try JevAPI.sanitized(Data(bad.utf8)))
    }
    func testSessionMatchAcceptsOnlyOfferedLabelsOrNone() throws {
        XCTAssertTrue(JevAPI.validChoices(["s1", "s2"]))
        XCTAssertFalse(JevAPI.validChoices([]))
        XCTAssertFalse(JevAPI.validChoices(["s1", "s1"]))
        XCTAssertFalse(JevAPI.validChoices(["11111111-1111-1111-1111-111111111111"]))
        let questions = JevAPI.sessionQuestions(["s1"])["session"] as? [String: Any]
        XCTAssertEqual(Set((questions?["criteria"] as? [String: String] ?? [:]).keys), ["none", "s1"])
        func answer(_ choice: String, _ confidence: Double) -> Data {
            Data(#"{"answers":{"session":{"type":"choice","choice":"\#(choice)","confidence":\#(confidence)}},"usage":{"input_tokens":5}}"#.utf8)
        }
        let ok = try JevAPI.sanitizedSession(answer("s1", 0.9), choices: ["s1", "s2"])
        XCTAssertEqual(ok["session"] as? String, "s1")
        XCTAssertEqual(ok["confidence"] as? Double, 0.9)
        XCTAssertEqual(try JevAPI.sanitizedSession(answer("none", 0.3), choices: ["s1"])["session"] as? String, "none")
        XCTAssertThrowsError(try JevAPI.sanitizedSession(answer("s3", 0.9), choices: ["s1", "s2"]))
        XCTAssertThrowsError(try JevAPI.sanitizedSession(answer("s1", 1.5), choices: ["s1"]))
    }
    func testDisabledStaleAndLowConfidenceKeepOriginalBehavior() throws {
        let helper = HomeOverviewTests()
        func snapshot(enabled: Bool, state: String, confidence: Double) throws -> StatusSnapshot {
            var value = try helper.snapshot([["insight": ["status":"disabled", "nextAction":"wait", "selection":
                ["status":state, "actor":"self", "action":"reply", "urgency":1.5, "confidence":confidence]]]])
            value.selectionSettings = .init(enabled: enabled, model: "jev-latest", registration: "g", keyReady: true)
            return value
        }
        XCTAssertEqual(HomeOverview(try snapshot(enabled: true, state: "ready", confidence: 0.9)).actions.first?.action, "질문 답변")
        for (enabled,state,confidence) in [(false,"ready",0.9),(true,"stale",0.9),(true,"ready",0.2)] {
            XCTAssertTrue(HomeOverview(try snapshot(enabled: enabled, state: state, confidence: confidence)).actions.isEmpty)
        }
    }
    func testJevWaitCannotHideCIFailureOrChangeRequest() throws {
        let helper = HomeOverviewTests()
        let decision: [String: Any] = ["status":"disabled", "nextAction":"wait", "selection": ["status":"ready", "actor":"other", "action":"wait", "urgency":0.2, "confidence":0.99]]
        var snapshot = try helper.snapshot([["checksState":"FAILURE", "insight":decision], ["number":2,"reviewDecision":"CHANGES_REQUESTED","insight":decision]])
        snapshot.selectionSettings = .init(enabled: true, model: "jev-latest", registration: "g", keyReady: true)
        let overview = HomeOverview(snapshot)
        XCTAssertEqual(overview.actions.count, 2)
        XCTAssertEqual(overview.actions.first?.action, "CI 실패 확인")
    }
}

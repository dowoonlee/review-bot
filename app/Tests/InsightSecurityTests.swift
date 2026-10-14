import XCTest
@testable import ReviewBotApp

final class InsightSecurityTests: XCTestCase {
    func testWorkspaceHeaderAndFixedDestination() throws {
        let input = InsightAPI.Input(action: "check", workspaceID: "wrkspc_test")
        let request = try InsightAPI.request(input, token: "dummy-key")
        XCTAssertEqual(request.url?.absoluteString, "https://api.anthropic.com/v1/models?limit=1")
        XCTAssertEqual(request.value(forHTTPHeaderField: "anthropic-workspace-id"), "wrkspc_test")
        let plain = try InsightAPI.request(.init(action: "check"), token: "dummy-key")
        XCTAssertNil(plain.value(forHTTPHeaderField: "anthropic-workspace-id"))
        XCTAssertThrowsError(try InsightAPI.request(.init(action: "check", workspaceID: "wrkspc_bad\r\nx-api-key:evil"), token: "dummy-key"))
        XCTAssertThrowsError(try InsightAPI.request(.init(action: "read-key"), token: "dummy-key"))
    }
    func testOnlySafeResponseFieldsLeaveProcess() throws {
        let data = Data(#"{"content":[{"type":"text","text":"dummy-key"}],"headers":{"x-api-key":"dummy-key"},"secret":"dummy-key","stop_reason":"dummy-key","usage":{"input_tokens":1,"output_tokens":2,"secret":"dummy-key"}}"#.utf8)
        let output = try InsightAPI.sanitized(data, token: "dummy-key")
        let encoded = try JSONSerialization.data(withJSONObject: output)
        XCTAssertFalse(String(decoding: encoded, as: UTF8.self).contains("dummy-key"))
        XCTAssertNil(output["headers"])
        XCTAssertNil(output["secret"])
    }
    func testRedirectIsRefused() {
        let session = URLSession(configuration: .ephemeral)
        defer { session.invalidateAndCancel() }
        let request = URLRequest(url: URL(string: "https://example.invalid")!)
        let task = session.dataTask(with: request)
        let response = HTTPURLResponse(url: request.url!, statusCode: 302, httpVersion: nil, headerFields: nil)!
        InsightAPI.RedirectBlocker().urlSession(session, task: task, willPerformHTTPRedirection: response,
            newRequest: request, completionHandler: { redirected in XCTAssertNil(redirected) })
    }
    func testLargerOutputAndTimeoutBudgetsSurviveNativeBridge() throws {
        let input = InsightAPI.Input(action: "analyze", model: "claude-sonnet-5-5", system: "Summarize", context: "Review", maxTokens: 16384, timeoutSec: 180)
        let request = try InsightAPI.request(input, token: "dummy-key")
        let body = try JSONSerialization.jsonObject(with: request.httpBody!) as! [String: Any]
        XCTAssertEqual(body["max_tokens"] as? Int, 16384)
        XCTAssertEqual(request.timeoutInterval, 180)
        let data = Data(#"{"stop_reason":"max_tokens","content":[],"usage":{}}"#.utf8)
        XCTAssertEqual(try InsightAPI.sanitized(data, token: "dummy-key")["stop_reason"] as? String, "max_tokens")
    }
    func testAnalyzeKeepsKeyOutOfBody() throws {
        let input = InsightAPI.Input(action: "analyze", model: "claude-sonnet-5-5", workspaceID: "wrkspc_test", system: "Summarize", context: "Review", maxTokens: 1800)
        let request = try InsightAPI.request(input, token: "dummy-key")
        XCTAssertEqual(request.url?.host, "api.anthropic.com")
        XCTAssertEqual(request.httpMethod, "POST")
        XCTAssertFalse(String(decoding: request.httpBody!, as: UTF8.self).contains("dummy-key"))
    }
}

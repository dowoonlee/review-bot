import Foundation
import Security

/// Only this signed process accesses the key. IPC accepts data, never URLs or headers.
enum InsightAPI {
    struct Input: Decodable {
        var action: String
        var model: String?
        var workspaceID: String?
        var system: String?
        var context: String?
        var maxTokens: Int?
        var timeoutSec: Int?
    }
    static func validWorkspace(_ value: String) -> Bool {
        value.isEmpty || value.range(of: #"^wrkspc_[A-Za-z0-9]{1,100}$"#, options: .regularExpression) != nil
    }
    static func request(_ input: Input, token: String) throws -> URLRequest {
        guard validWorkspace(input.workspaceID ?? ""), ["check", "analyze"].contains(input.action) else { throw Failure.invalid }
        let path = input.action == "check" ? "models?limit=1" : "messages"
        var request = URLRequest(url: URL(string: "https://api.anthropic.com/v1/" + path)!)
        request.timeoutInterval = Double(max(5, min(180, input.timeoutSec ?? 90)))
        request.setValue(token, forHTTPHeaderField: "x-api-key")
        request.setValue("2023-06-01", forHTTPHeaderField: "anthropic-version")
        if let workspace = input.workspaceID, !workspace.isEmpty {
            request.setValue(workspace, forHTTPHeaderField: "anthropic-workspace-id")
        }
        if input.action == "analyze" {
            guard let model = input.model, model.range(of: #"^claude-[A-Za-z0-9.-]{1,90}$"#, options: .regularExpression) != nil,
                  let system = input.system, let context = input.context,
                  system.utf8.count <= 10000, context.utf8.count <= 300000 else { throw Failure.invalid }
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONSerialization.data(withJSONObject: [
                "model": model, "max_tokens": max(256, min(16384, input.maxTokens ?? 8192)),
                "system": system, "messages": [["role": "user", "content": context]]])
        }
        return request
    }
    enum Failure: Error { case invalid }
    final class RedirectBlocker: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
        func urlSession(_ session: URLSession, task: URLSessionTask,
                        willPerformHTTPRedirection response: HTTPURLResponse,
                        newRequest request: URLRequest,
                        completionHandler: @escaping @Sendable (URLRequest?) -> Void) {
            completionHandler(nil)
        }
    }
    /// Strip everything except model output and usage. Never relay API error bodies or headers.
    static func sanitized(_ data: Data, token: String) throws -> [String: Any] {
        guard let raw = try JSONSerialization.jsonObject(with: data) as? [String: Any] else { throw Failure.invalid }
        let content = (raw["content"] as? [[String: Any]] ?? []).compactMap { block -> [String: String]? in
            guard block["type"] as? String == "text", let text = block["text"] as? String else { return nil }
            return ["type": "text", "text": text.replacingOccurrences(of: token, with: "[redacted]")]
        }
        let usage = raw["usage"] as? [String: Any] ?? [:]
        let stop = raw["stop_reason"] as? String ?? ""
        return ["content": content, "stop_reason": ["end_turn", "max_tokens", "stop_sequence", "refusal", "pause_turn"].contains(stop) ? stop : "incomplete",
                "usage": ["input_tokens": usage["input_tokens"] as? Int ?? 0,
                          "output_tokens": usage["output_tokens"] as? Int ?? 0]]
    }
    static func run() async -> [String: Any] {
        do {
            var data = Data()
            while let chunk = try FileHandle.standardInput.read(upToCount: 16384), !chunk.isEmpty {
                data.append(chunk)
                if data.count > 320000 { throw Failure.invalid }
            }
            guard data.count <= 320000,
                  let object = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                  Set(object.keys).isSubset(of: ["action", "model", "workspaceID", "system", "context", "maxTokens", "timeoutSec"]) else { throw Failure.invalid }
            let input = try JSONDecoder().decode(Input.self, from: data)
            guard ["status", "check", "analyze"].contains(input.action) else { throw Failure.invalid }
            let credential = InsightKeychain.backgroundRead(verified: InsightKeychain.accessVerified) {
                InsightKeychain.readResult()
            }
            if input.action == "status" {
                return ["keyState": credential.status == errSecSuccess ? "ok" :
                    credential.status == errSecItemNotFound ? "missing" : "locked"]
            }
            guard credential.status == errSecSuccess else {
                return ["error": InsightKeychain.errorCode(credential.status)]
            }
            guard let bytes = credential.data, let token = String(data: bytes, encoding: .utf8), !token.isEmpty else {
                return ["error": "missing_key"]
            }
            let request = try request(input, token: token)
            let configuration = URLSessionConfiguration.ephemeral
            configuration.urlCache = nil
            configuration.httpCookieStorage = nil
            configuration.urlCredentialStorage = nil
            configuration.timeoutIntervalForResource = request.timeoutInterval
            let session = URLSession(configuration: configuration, delegate: RedirectBlocker(), delegateQueue: nil)
            defer { session.invalidateAndCancel() }
            let (body, response) = try await session.data(for: request)
            guard let http = response as? HTTPURLResponse else { return ["error": "network"] }
            guard (200..<300).contains(http.statusCode) else { return ["error": "http", "status": http.statusCode] }
            if input.action == "check" { return ["ok": true] }
            return try sanitized(body, token: token)
        } catch is Failure { return ["error": "invalid_request"] }
        catch { return ["error": "network_or_response"] }
    }
}

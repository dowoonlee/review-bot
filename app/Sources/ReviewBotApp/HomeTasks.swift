import Foundation

struct HomeTask: Decodable, Sendable, Identifiable {
    var id: String
    var requestID: String
    var repo: String
    var number: Int
    var title: String
    var label: String
    var status: String
    var agent: String
    var sessionID: String?
    var terminalID: String?
    var startedAt: String
    var completedAt: String?
    var reopenReason: String?
    var lastSyncError: String?
}
struct HomeTaskFile: Decodable, Sendable {
    var tasks: [String: HomeTask]
    var current: [String: String]
    static let empty = HomeTaskFile(tasks: [:], current: [:])
    func currentTask(for requestID: String) -> HomeTask? {
        current[requestID].flatMap { tasks[$0] }
    }
    var completed: [HomeTask] {
        current.values.compactMap { tasks[$0] }.filter { $0.status == "completed" }
            .sorted { ($0.completedAt ?? "") > ($1.completedAt ?? "") }
    }
}

enum HomeTaskClient {
    struct Failure: Error, LocalizedError {
        let message: String
        var errorDescription: String? { message }
    }
    static func start(_ row: HomeRequest, agent: String, sessionID: String? = nil, terminalID: String? = nil) async throws -> HomeTask {
        var arguments = [BotPaths.executable.path, "task", "start", "\(row.repo)#\(row.number)",
            "--account", row.account ?? "global", "--direction", row.id.hasPrefix("incoming:") ? "incoming" : "outgoing",
            "--title", row.title, "--label", row.action, "--agent", agent]
        if let sessionID { arguments += ["--session-id", sessionID] }
        if let terminalID { arguments += ["--terminal-id", terminalID] }
        let result = await Shell.run(arguments, cwd: BotPaths.root.path)
        guard result.code == 0, let task = try? JSONDecoder().decode(HomeTask.self, from: Data(result.output.utf8)) else {
            throw Failure(message: result.output.isEmpty ? "작업을 등록하지 못했습니다." : result.output)
        }
        return task
    }
    static func command(_ action: String, id: String) async -> Shell.Result {
        await Shell.run([BotPaths.executable.path, "task", action, id], cwd: BotPaths.root.path)
    }
    static func completionCommand(_ id: String) -> String {
        AgentTerminal.quote(BotPaths.executable.path) + " task complete " + AgentTerminal.quote(id)
    }
}

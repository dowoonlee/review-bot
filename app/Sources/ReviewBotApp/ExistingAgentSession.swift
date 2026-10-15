import AppKit
import Foundation

enum ExistingAgentSession {
    struct Session: Identifiable, Sendable {
        let id: String
        let tty: String
        let name: String
    }

    static let listScript = """
    tell application id "com.googlecode.iterm2"
        set sessionRows to ""
        repeat with w in windows
            try
                repeat with t in tabs of w
                    try
                        repeat with s in sessions of t
                            try
                                set sessionRows to sessionRows & (id of s) & (character id 9) & (tty of s) & (character id 9) & (name of s) & linefeed
                            end try
                        end repeat
                    end try
                end repeat
            end try
        end repeat
        return sessionRows
    end tell
    """

    static func parseSessions(_ output: String) -> [Session] {
        output.split(separator: "\n").compactMap { line in
            let fields = line.split(separator: "\t", maxSplits: 2, omittingEmptySubsequences: false).map(String.init)
            guard fields.count == 3, let id = UUID(uuidString: fields[0]),
                  fields[1].range(of: "^/dev/ttys[0-9]+$", options: .regularExpression) != nil else { return nil }
            return Session(id: id.uuidString.lowercased(), tty: fields[1], name: fields[2])
        }
    }

    /// Only the foreground CLI counts; a daemon or a background CLI is not an input target.
    static func hasAgent(_ agent: AgentTerminal.Agent, tty: String, processes: String) -> Bool {
        let terminal = String(tty.dropFirst("/dev/".count))
        return processes.split(separator: "\n").contains { line in
            let fields = line.split(maxSplits: 2, whereSeparator: { $0.isWhitespace }).map(String.init)
            return fields.count == 3 && fields[0] == terminal && fields[1].contains("+")
                && (fields[2].trimmingCharacters(in: .whitespaces) as NSString).lastPathComponent == agent.rawValue
        }
    }

    static func available(_ agent: AgentTerminal.Agent) async throws -> [Session] {
        guard await MainActor.run(body: {
            NSRunningApplication.runningApplications(withBundleIdentifier: "com.googlecode.iterm2").isEmpty == false
        }) else { throw HomeTaskClient.Failure(message: "iTerm을 먼저 열어 주세요.") }
        let result = await Shell.run(["/usr/bin/osascript", "-e", listScript])
        guard result.code == 0 else {
            throw HomeTaskClient.Failure(message: "iTerm 세션을 읽지 못했습니다. 시스템 설정의 자동화에서 ReviewBot의 iTerm 제어를 허용해 주세요.")
        }
        let processes = await Shell.run(["/bin/ps", "-axo", "tty=,stat=,comm="])
        guard processes.code == 0 else { throw HomeTaskClient.Failure(message: "실행 중인 CLI를 확인하지 못했습니다.") }
        return parseSessions(result.output).filter { hasAgent(agent, tty: $0.tty, processes: processes.output) }
    }

    // Bracketed paste preserves multiline input. Remove terminal controls from PR-supplied text.
    static func pastePayload(_ prompt: String) -> String {
        let clean = String(prompt.unicodeScalars.filter {
            $0.value == 10 || $0.value == 9 || !CharacterSet.controlCharacters.contains($0)
        })
        return "\u{1B}[200~" + clean + "\u{1B}[201~"
    }

    static let sendScript = """
    on run argv
        set targetID to item 1 of argv
        set targetTTY to item 2 of argv
        set payload to item 3 of argv
        set checkCommand to item 4 of argv
        tell application id "com.googlecode.iterm2"
            repeat with w in windows
                repeat with t in tabs of w
                    repeat with s in sessions of t
                        ignoring case
                            set matches to (id of s) is targetID
                        end ignoring
                        if matches and (tty of s) is targetTTY then
                            if (is at shell prompt of s) then error "CLI가 종료되어 전송하지 않았습니다."
                            if (do shell script checkCommand) is not "ready" then error "선택한 CLI가 실행 중이 아닙니다."
                            tell s
                                write text (payload & (character id 13)) newline false
                            end tell
                            return "sent"
                        end if
                    end repeat
                end repeat
            end repeat
        end tell
        error "연결한 iTerm 세션이 닫혔습니다. 다시 선택해 주세요."
    end run
    """

    static func foregroundCheck(_ agent: AgentTerminal.Agent, tty: String) -> String {
        // tty comes only from parseSessions; the agent is an enum, never user-provided shell text.
        "/bin/ps -t " + AgentTerminal.quote(String(tty.dropFirst("/dev/".count)))
            + " -o stat=,comm= | /usr/bin/awk "
            + AgentTerminal.quote("$1 ~ /\\+/ { n=split($2,p,\"/\"); if (p[n] == \"\(agent.rawValue)\") found=1 } END { if (found) print \"ready\" }")
    }

    static func send(_ agent: AgentTerminal.Agent, terminalID: String, prompt: String) async throws {
        guard let session = try await available(agent).first(where: { $0.id == terminalID.lowercased() }) else {
            throw HomeTaskClient.Failure(message: "연결한 탭에서 \(agent.label)가 실행 중인지 확인하고 다시 선택해 주세요.")
        }
        let result = await Shell.run(["/usr/bin/osascript", "-e", sendScript,
            session.id, session.tty, pastePayload(prompt), foregroundCheck(agent, tty: session.tty)])
        guard result.code == 0, result.output.trimmingCharacters(in: .whitespacesAndNewlines) == "sent" else {
            throw HomeTaskClient.Failure(message: "전송을 확인하지 못했습니다. 중복 전송을 피하려면 해당 탭을 확인해 주세요. " + result.output)
        }
    }
}

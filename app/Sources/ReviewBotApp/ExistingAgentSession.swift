import AppKit
import Foundation

enum ExistingAgentSession {
    struct Session: Identifiable, Sendable {
        let id: String
        let tty: String
        let name: String
        /// 아래는 `availableAll` 이 채운다 — 전경 CLI 종류와 그 작업 디렉터리의 git 저장소·브랜치.
        var agent: AgentTerminal.Agent? = nil
        var directory: String? = nil
        var repo: String? = nil
        var branch: String? = nil
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

    /// tty → 전경 CLI 와 pid. `ps -axo tty=,stat=,pid=,comm=` 출력을 읽는다.
    static func foregroundCLIs(_ processes: String) -> [String: (agent: AgentTerminal.Agent, pid: Int)] {
        var result: [String: (agent: AgentTerminal.Agent, pid: Int)] = [:]
        for line in processes.split(separator: "\n") {
            let fields = line.split(maxSplits: 3, whereSeparator: { $0.isWhitespace }).map(String.init)
            guard fields.count == 4, fields[1].contains("+"), let pid = Int(fields[2]),
                  let agent = AgentTerminal.Agent(rawValue: (fields[3].trimmingCharacters(in: .whitespaces) as NSString).lastPathComponent)
            else { continue }
            result[fields[0]] = (agent, pid)
        }
        return result
    }

    /// origin URL 에서 owner/repo. https·ssh·scp 꼴을 받고 GitHub 가 아니면 nil.
    static func repoName(fromRemote url: String) -> String? {
        let value = url.trimmingCharacters(in: .whitespacesAndNewlines)
        let pattern = #"github\.com[:/]([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$"#
        guard let regex = try? NSRegularExpression(pattern: pattern),
              let match = regex.firstMatch(in: value, range: NSRange(value.startIndex..., in: value)),
              let range = Range(match.range(at: 1), in: value) else { return nil }
        return String(value[range])
    }

    /// Codex·Claude 탭을 한 번에 — 추천 근거로 CLI 의 작업 디렉터리와 그 git 저장소·브랜치를 읽는다.
    /// 화면에 보이는 텍스트는 읽지 않는다.
    static func availableAll() async throws -> [Session] {
        guard await MainActor.run(body: {
            NSRunningApplication.runningApplications(withBundleIdentifier: "com.googlecode.iterm2").isEmpty == false
        }) else { throw HomeTaskClient.Failure(message: "iTerm을 먼저 열어 주세요.") }
        let result = await Shell.run(["/usr/bin/osascript", "-e", listScript])
        guard result.code == 0 else {
            throw HomeTaskClient.Failure(message: "iTerm 세션을 읽지 못했습니다. 시스템 설정의 자동화에서 ReviewBot의 iTerm 제어를 허용해 주세요.")
        }
        let processes = await Shell.run(["/bin/ps", "-axo", "tty=,stat=,pid=,comm="])
        guard processes.code == 0 else { throw HomeTaskClient.Failure(message: "실행 중인 CLI를 확인하지 못했습니다.") }
        let clis = foregroundCLIs(processes.output)
        var sessions: [Session] = []
        for var session in parseSessions(result.output) {
            guard let cli = clis[String(session.tty.dropFirst("/dev/".count))] else { continue }
            session.agent = cli.agent
            let cwd = await Shell.run(["/usr/sbin/lsof", "-a", "-p", String(cli.pid), "-d", "cwd", "-Fn"])
            if cwd.code == 0, let line = cwd.output.split(separator: "\n").first(where: { $0.hasPrefix("n/") }) {
                let directory = String(line.dropFirst())
                session.directory = directory
                let branch = await Shell.run(["/usr/bin/git", "-C", directory, "rev-parse", "--abbrev-ref", "HEAD"])
                let name = branch.output.trimmingCharacters(in: .whitespacesAndNewlines)
                if branch.code == 0, !name.isEmpty, name != "HEAD" { session.branch = name }
                let remote = await Shell.run(["/usr/bin/git", "-C", directory, "remote", "get-url", "origin"])
                if remote.code == 0 { session.repo = repoName(fromRemote: remote.output) }
            }
            sessions.append(session)
        }
        return sessions
    }

    /// 할 일에 연결된 iTerm 세션이 아직 살아 있는지 — 탭이 남아 있고 그 tty 에서 같은 CLI 가
    /// 전경으로 돌고 있어야 산 것이다. 탭이 닫혔거나 CLI 가 빠져나와 셸 프롬프트만 남았으면 죽은 것.
    struct Link: Sendable {
        let taskID: String
        let agent: AgentTerminal.Agent
        let terminalID: String
    }

    static func liveness(_ links: [Link], sessions: [Session], processes: String) -> [String: Bool] {
        var result: [String: Bool] = [:]
        for link in links {
            let session = sessions.first { $0.id == link.terminalID.lowercased() }
            result[link.taskID] = session.map { hasAgent(link.agent, tty: $0.tty, processes: processes) } ?? false
        }
        return result
    }

    /// iTerm 이 꺼져 있으면 연결된 탭은 전부 죽은 것이다 — 세션 ID 는 iTerm 실행마다 새로 난다.
    /// 자동화 권한이 없어 목록을 못 읽으면 모른다고 던진다. 그때는 아무것도 바꾸지 않는다.
    static func liveness(_ links: [Link]) async throws -> [String: Bool] {
        guard !links.isEmpty else { return [:] }
        let running = await MainActor.run {
            !NSRunningApplication.runningApplications(withBundleIdentifier: "com.googlecode.iterm2").isEmpty
        }
        guard running else { return Dictionary(uniqueKeysWithValues: links.map { ($0.taskID, false) }) }
        let result = await Shell.run(["/usr/bin/osascript", "-e", listScript])
        guard result.code == 0 else {
            throw HomeTaskClient.Failure(message: "iTerm 세션을 읽지 못해 연결 상태를 확인하지 못했습니다. 시스템 설정의 자동화에서 ReviewBot의 iTerm 제어를 허용해 주세요.")
        }
        let processes = await Shell.run(["/bin/ps", "-axo", "tty=,stat=,comm="])
        guard processes.code == 0 else { throw HomeTaskClient.Failure(message: "실행 중인 CLI를 확인하지 못했습니다.") }
        return liveness(links, sessions: parseSessions(result.output), processes: processes.output)
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

    static let focusScript = """
    on run argv
        set targetID to item 1 of argv
        tell application id "com.googlecode.iterm2"
            repeat with w in windows
                repeat with t in tabs of w
                    repeat with s in sessions of t
                        ignoring case
                            set matches to (id of s) is targetID
                        end ignoring
                        if matches then
                            select w
                            select t
                            select s
                            activate
                            return "focused"
                        end if
                    end repeat
                end repeat
            end repeat
        end tell
        error "연결한 iTerm 세션이 닫혔습니다. 다시 선택해 주세요."
    end run
    """

    static func focus(terminalID: String) async throws {
        guard let id = UUID(uuidString: terminalID) else {
            throw HomeTaskClient.Failure(message: "연결한 iTerm 세션 ID가 올바르지 않습니다. 다시 선택해 주세요.")
        }
        guard await MainActor.run(body: {
            !NSRunningApplication.runningApplications(withBundleIdentifier: "com.googlecode.iterm2").isEmpty
        }) else { throw HomeTaskClient.Failure(message: "iTerm이 실행 중이 아닙니다. 연결할 세션을 다시 열어 주세요.") }
        let result = await Shell.run(["/usr/bin/osascript", "-e", focusScript, id.uuidString.lowercased()])
        guard result.code == 0, result.output.trimmingCharacters(in: .whitespacesAndNewlines) == "focused" else {
            throw HomeTaskClient.Failure(message: "연결한 세션으로 이동하지 못했습니다. iTerm 세션과 자동화 권한을 확인해 주세요. " + result.output)
        }
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

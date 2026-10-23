import AppKit
import XCTest
@testable import ReviewBotApp

final class ExistingAgentSessionTests: XCTestCase {
    func testSessionListRequiresExactIDsAndTerminalPaths() {
        let id = "12345678-1234-5678-9ABC-123456789ABC"
        let sessions = ExistingAgentSession.parseSessions("\(id)\t/dev/ttys004\tCodex · repo\ninvalid\t/dev/ttys000\tbad\n\(id)\t/dev/ttys000;echo\tbad")
        XCTAssertEqual(sessions.count, 1)
        XCTAssertEqual(sessions[0].id, id.lowercased())
        XCTAssertEqual(sessions[0].name, "Codex · repo")
    }

    func testOnlyMatchingForegroundCLIIsEligible() {
        let processes = """
        ttys001  S+   /usr/local/bin/codex
        ttys002 S claude
        ttys003  S+   claude
        ?? S+ codex
        ttys004 S+ codex-helper
        """
        XCTAssertTrue(ExistingAgentSession.hasAgent(.codex, tty: "/dev/ttys001", processes: processes))
        XCTAssertFalse(ExistingAgentSession.hasAgent(.claude, tty: "/dev/ttys001", processes: processes))
        XCTAssertFalse(ExistingAgentSession.hasAgent(.claude, tty: "/dev/ttys002", processes: processes))
        XCTAssertTrue(ExistingAgentSession.hasAgent(.claude, tty: "/dev/ttys003", processes: processes))
        XCTAssertFalse(ExistingAgentSession.hasAgent(.codex, tty: "/dev/ttys004", processes: processes))
    }

    func testAlignedProcessColumnsAndExecutablePathsWithSpaces() {
        let rows = "ttys000  S+   claude\nttys004  S+   codex\nttys005  S+   /Applications/My Tools/bin/codex\n"
        XCTAssertTrue(ExistingAgentSession.hasAgent(.claude, tty: "/dev/ttys000", processes: rows))
        XCTAssertTrue(ExistingAgentSession.hasAgent(.codex, tty: "/dev/ttys004", processes: rows))
        XCTAssertTrue(ExistingAgentSession.hasAgent(.codex, tty: "/dev/ttys005", processes: rows))
    }

    func testLivenessNeedsTheTabAndTheForegroundCLI() {
        let open = "12345678-1234-5678-9abc-123456789abc"
        let idle = "22345678-1234-5678-9abc-123456789abc"
        let sessions = ExistingAgentSession.parseSessions("\(open)\t/dev/ttys001\tClaude\n\(idle)\t/dev/ttys002\tshell\n")
        let processes = "ttys001  S+   claude\nttys002  S+   -zsh\n"
        let links = [
            ExistingAgentSession.Link(taskID: "t-open", agent: .claude, terminalID: open.uppercased()),
            ExistingAgentSession.Link(taskID: "t-wrong-agent", agent: .codex, terminalID: open),
            ExistingAgentSession.Link(taskID: "t-idle", agent: .claude, terminalID: idle),
            ExistingAgentSession.Link(taskID: "t-closed", agent: .claude, terminalID: "32345678-1234-5678-9abc-123456789abc"),
        ]
        let alive = ExistingAgentSession.liveness(links, sessions: sessions, processes: processes)
        XCTAssertEqual(alive, ["t-open": true, "t-wrong-agent": false, "t-idle": false, "t-closed": false])
        XCTAssertEqual(ExistingAgentSession.liveness([], sessions: sessions, processes: processes), [:])
    }

    func testPasteRetainsLiteralTextButRemovesEmbeddedTerminalControls() {
        let payload = ExistingAgentSession.pastePayload("한글\n$(touch nope)\t\u{1B}[201~\r\u{03}test")
        XCTAssertEqual(payload, "\u{1B}[200~한글\n$(touch nope)\t[201~test\u{1B}[201~")
        XCTAssertEqual(payload.filter { $0 == "\u{1B}" }.count, 2)
    }

    func testRunningITermReturnsParseableSessionRows() async throws {
        let running = await MainActor.run {
            !NSRunningApplication.runningApplications(withBundleIdentifier: "com.googlecode.iterm2").isEmpty
        }
        guard running else { throw XCTSkip("iTerm is not running") }
        let result = await Shell.run(["/usr/bin/osascript", "-e", ExistingAgentSession.listScript])
        XCTAssertEqual(result.code, 0, result.output)
        guard result.code == 0 else { return }
        let output = result.output.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !output.isEmpty else { throw XCTSkip("No open iTerm sessions") }
        let sessions = ExistingAgentSession.parseSessions(output)
        XCTAssertFalse(sessions.isEmpty, "iTerm returned rows but none could be parsed")
        XCTAssertTrue(output.contains("\t"), "iTerm's tab class must not replace the delimiter")
    }

    func testAppleScriptsCompileWithoutSendingAnything() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        for (index, script) in [ExistingAgentSession.listScript, ExistingAgentSession.sendScript, ExistingAgentSession.focusScript].enumerated() {
            let result = await Shell.run(["/usr/bin/osacompile", "-o", directory.appendingPathComponent("\(index).scpt").path, "-e", script])
            XCTAssertEqual(result.code, 0, result.output)
        }
    }
}

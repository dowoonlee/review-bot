import XCTest
@testable import ReviewBotApp

final class AgentTerminalTests: XCTestCase {
    func testBothCommandsReceiveLiteralPromptAndRepository() throws {
        let fm = FileManager.default
        let root = fm.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? fm.removeItem(at: root) }
        let repo = root.appendingPathComponent("repo ' $(echo nope)")
        let bin = root.appendingPathComponent("bin")
        try fm.createDirectory(at: repo, withIntermediateDirectories: true)
        try fm.createDirectory(at: bin, withIntermediateDirectories: true)
        let marker = root.appendingPathComponent("must-not-exist")
        let prompt = "PR 'quoted'\n$(touch \(marker.path)) `touch \(marker.path)`\n여러 줄 분석"
        for agent in AgentTerminal.Agent.allCases {
            let stub = bin.appendingPathComponent(agent.rawValue)
            try """
            #!/bin/sh
            /bin/pwd > "$REVIEWBOT_TEST_CAPTURE/cwd"
            printf '%s' "$#" > "$REVIEWBOT_TEST_CAPTURE/argc"
            if [ "$1" = '--yolo' ]; then
              printf '%s' "$1" > "$REVIEWBOT_TEST_CAPTURE/flag"
              shift
            fi
            printf '%s' "$1" > "$REVIEWBOT_TEST_CAPTURE/prompt"
            """.write(to: stub, atomically: true, encoding: .utf8)
            try fm.setAttributes([.posixPermissions: 0o700], ofItemAtPath: stub.path)
            let file = root.appendingPathComponent("prompt ' $(echo nope).txt")
            try prompt.write(to: file, atomically: true, encoding: .utf8)
            let process = Process()
            process.executableURL = URL(fileURLWithPath: "/bin/zsh")
            process.arguments = ["-c", AgentTerminal.script(agent: agent, repository: repo.path, promptFile: file.path)]
            process.environment = ["PATH": bin.path + ":/usr/bin:/bin", "REVIEWBOT_TEST_CAPTURE": root.path]
            try process.run(); process.waitUntilExit()
            XCTAssertEqual(process.terminationStatus, 0)
            XCTAssertEqual(try String(contentsOf: root.appendingPathComponent("prompt"), encoding: .utf8), prompt)
            let cwd = try String(contentsOf: root.appendingPathComponent("cwd"), encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines)
            XCTAssertEqual(URL(fileURLWithPath: cwd).resolvingSymlinksInPath(), repo.resolvingSymlinksInPath())
            XCTAssertEqual(try String(contentsOf: root.appendingPathComponent("argc"), encoding: .utf8), agent == .codex ? "2" : "1")
            if agent == .codex { XCTAssertEqual(try String(contentsOf: root.appendingPathComponent("flag"), encoding: .utf8), "--yolo") }
            XCTAssertFalse(fm.fileExists(atPath: marker.path))
            XCTAssertFalse(fm.fileExists(atPath: file.path))
        }
    }
    func testOnlyCurrentAnalysisIsHandedOff() throws {
        let helper = HomeOverviewTests()
        for state in ["ready", "stale", "error"] {
            let row = HomeOverview(try helper.snapshot([["insight": helper.insight(state)]])).requests[0]
            let text = AgentTerminal.prompt(for: row)
            XCTAssertTrue(text.contains("https://github.com/org/repo/pull/1"))
            XCTAssertEqual(text.contains("A question"), state == "ready")
            XCTAssertTrue(text.contains("AGENTS.md"))
        }
    }
}

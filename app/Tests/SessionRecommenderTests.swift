import XCTest
@testable import ReviewBotApp

final class SessionRecommenderTests: XCTestCase {
    typealias Session = ExistingAgentSession.Session
    let a = "11111111-1111-1111-1111-111111111111"
    let b = "22222222-2222-2222-2222-222222222222"
    let c = "33333333-3333-3333-3333-333333333333"

    func request(head: String? = "feature/x") -> HomeRequest {
        HomeRequest(id: "outgoing:me:org/repo#7", repo: "org/repo", number: 7, title: "Fix", action: "수정 요청 검토", reason: "",
                    priority: 1, waiting: false, direction: "보낸 요청", selection: .sent("me:org/repo#7"), insight: nil,
                    stamp: HomeStamp(state: "", checks: "", conflict: false, revision: "", analysis: ""), account: "me", headRef: head)
    }
    func session(_ id: String, _ agent: AgentTerminal.Agent, repo: String? = "org/repo", branch: String? = nil) -> Session {
        Session(id: id, tty: "/dev/ttys001", name: "tab", agent: agent, directory: "/work", repo: repo, branch: branch)
    }
    func tasks(_ rows: [(id: String, terminal: String, agent: String, started: String)], current: String? = nil) -> HomeTaskFile {
        var file = HomeTaskFile(tasks: [:], current: [:])
        for row in rows {
            file.tasks[row.id] = HomeTask(id: row.id, requestID: "outgoing:me:org/repo#7", repo: "org/repo", number: 7, title: "Fix",
                                          label: "", status: "superseded", agent: row.agent, terminalID: row.terminal, startedAt: row.started)
        }
        if let current { file.current["outgoing:me:org/repo#7"] = current }
        return file
    }

    func testPreviouslyLinkedLiveTabWinsAndPrefersTheCurrentTask() {
        let sessions = [session(a, .codex, branch: "feature/x"), session(b, .claude), session(c, .codex)]
        let file = tasks([("t1", b.uppercased(), "claude", "2026-10-20"), ("t2", c, "codex", "2026-10-22")], current: "t1")
        XCTAssertEqual(SessionRecommender.deterministic(row: request(), sessions: sessions, tasks: file),
                       SessionRecommendation(terminalID: b, source: .linked, confidence: nil))
        let newest = tasks([("t1", b, "claude", "2026-10-20"), ("t2", c, "codex", "2026-10-22")])
        XCTAssertEqual(SessionRecommender.deterministic(row: request(), sessions: sessions, tasks: newest)?.terminalID, c)
    }

    func testLinkedTabMustStillRunTheSameCLIOtherwiseBranchDecides() {
        let sessions = [session(a, .codex, branch: "feature/x"), session(b, .codex)]
        let file = tasks([("t1", b, "claude", "2026-10-20")], current: "t1")
        XCTAssertEqual(SessionRecommender.deterministic(row: request(), sessions: sessions, tasks: file),
                       SessionRecommendation(terminalID: a, source: .branch, confidence: nil))
    }

    func testBranchMatchNeedsTheSameRepositoryAndMustBeUnique() {
        let other = [session(a, .codex, repo: "org/other", branch: "feature/x"), session(b, .claude, repo: "ORG/Repo", branch: "feature/x")]
        XCTAssertEqual(SessionRecommender.deterministic(row: request(), sessions: other, tasks: .empty)?.terminalID, b)
        let ambiguous = [session(a, .codex, branch: "feature/x"), session(b, .claude, branch: "feature/x")]
        XCTAssertNil(SessionRecommender.deterministic(row: request(), sessions: ambiguous, tasks: .empty))
        XCTAssertNil(SessionRecommender.deterministic(row: request(head: nil), sessions: [session(a, .codex, branch: "feature/x")], tasks: .empty))
    }

    func testJevContextCarriesOnlyNamesPathsAndBranches() {
        let sessions = [session(a, .codex, branch: "feature/x"), session(b, .claude, repo: nil)]
        let labels = SessionRecommender.labels(for: sessions)
        XCTAssertEqual(labels, ["s1", "s2"])
        let context = SessionRecommender.context(row: request(), sessions: sessions, labels: labels)
        XCTAssertTrue(context.contains("org/repo#7"))
        XCTAssertTrue(context.contains("- s1: cli=Codex | tab=tab | directory=/work | repository=org/repo | branch=feature/x"))
        XCTAssertTrue(context.contains("- s2: cli=Claude | tab=tab | directory=/work"))
        XCTAssertFalse(context.contains(a), "terminal ids stay local")
    }

    func testJevPickNeedsConfidenceAndAKnownLabel() {
        let sessions = [session(a, .codex), session(b, .claude)]
        let labels = SessionRecommender.labels(for: sessions)
        XCTAssertEqual(SessionRecommender.recommendation(from: ["session": "s2", "confidence": 0.9], sessions: sessions, labels: labels),
                       SessionRecommendation(terminalID: b, source: .jev, confidence: 0.9))
        XCTAssertNil(SessionRecommender.recommendation(from: ["session": "s2", "confidence": 0.5], sessions: sessions, labels: labels))
        XCTAssertNil(SessionRecommender.recommendation(from: ["session": "none", "confidence": 0.99], sessions: sessions, labels: labels))
        XCTAssertNil(SessionRecommender.recommendation(from: ["error": "network"], sessions: sessions, labels: labels))
    }
}

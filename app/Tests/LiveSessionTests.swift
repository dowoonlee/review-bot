import XCTest
@testable import ReviewBotApp

final class LiveSessionTests: XCTestCase {
    private func session(_ id: String, _ agent: String, at seconds: TimeInterval,
                         pr: String = "skcc-ai/c2-10124") -> LiveSession {
        LiveSession(id: id, agent: agent, pr: pr, turns: 0, tools: [:], lastTool: "", lastDetail: "",
                    updatedAt: Date(timeIntervalSince1970: seconds), deniedTools: 0)
    }

    private func run(_ repo: String, _ number: Int, startedAt: String?) -> CurrentRun {
        CurrentRun(repo: repo, number: number, startedAt: startedAt)
    }

    func testCutoffIsTheRunStartOnlyForItsOwnWorktree() {
        let current = run("skcc-ai/c2", 10124, startedAt: "2026-11-13T15:38:54")
        XCTAssertEqual(LiveSession.cutoff(worktree: "skcc-ai__c2-10124", current: current),
                       RunRecord.stamp.date(from: "2026-11-13T15:38:54"))
        XCTAssertNil(LiveSession.cutoff(worktree: "skcc-ai__c2-1012", current: current))
        XCTAssertNil(LiveSession.cutoff(worktree: "skcc-ai__c2-10124", current: nil))
        XCTAssertNil(LiveSession.cutoff(worktree: "skcc-ai__c2-10124",
                                        current: run("skcc-ai/c2", 10124, startedAt: nil)))
    }

    func testRetriesOfOneTaskCollapseToTheLatest() {
        let kept = LiveSession.latestPerTask([
            session("a", "code-reviewer · 묶음 3", at: 10),
            session("b", "code-reviewer · 묶음 3", at: 30),
            session("c", "code-reviewer · 묶음 3", at: 20),
            session("d", "security-reviewer · 묶음 3", at: 25),
        ])
        XCTAssertEqual(kept.map(\.id), ["b", "d"])
    }

    func testSameTaskOnAnotherPRAndUnnamedSessionsStayApart() {
        let kept = LiveSession.latestPerTask([
            session("a", "최종 판정", at: 10),
            session("b", "최종 판정", at: 20, pr: "skcc-ai/c2-10107"),
            session("c", SessionLabel.pending, at: 30),
            session("d", SessionLabel.pending, at: 40),
        ])
        XCTAssertEqual(kept.map(\.id), ["d", "c", "b", "a"])
    }
}

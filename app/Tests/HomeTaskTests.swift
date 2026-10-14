import XCTest
@testable import ReviewBotApp

final class HomeTaskTests: XCTestCase {
    func record(status: String, requestID: String) throws -> HomeTask {
        let value: [String: Any] = ["id":"task-1","requestID":requestID,"repo":"org/repo","number":1,
            "title":"Fix","label":"리뷰 대응","status":status,"agent":"codex","startedAt":"now","completedAt":"later"]
        return try JSONDecoder().decode(HomeTask.self, from: JSONSerialization.data(withJSONObject:value))
    }
    func testCompletionHidesActionAndReopeningRestoresIt() throws {
        let snapshot = try HomeOverviewTests().snapshot([["reviewDecision":"CHANGES_REQUESTED"]])
        let key = HomeOverview(snapshot).requests[0].id
        for status in ["running","stopped","completed","reopened"] {
            let task = try record(status: status, requestID: key)
            let file = HomeTaskFile(tasks:[task.id:task],current:[key:task.id])
            let overview = HomeOverview(snapshot,tasks:file)
            XCTAssertEqual(overview.actions.isEmpty,status == "completed")
            XCTAssertEqual(file.completed.count,status == "completed" ? 1 : 0)
        }
    }
    func testExplicitlyReopenedTaskRemainsVisibleEvenIfModelSaysWait() throws {
        let snapshot = try HomeOverviewTests().snapshot([[:]])
        let key = HomeOverview(snapshot).requests[0].id
        let task = try record(status:"reopened",requestID:key)
        let overview = HomeOverview(snapshot,tasks:HomeTaskFile(tasks:[task.id:task],current:[key:task.id]))
        XCTAssertEqual(overview.actions.count,1)
        XCTAssertTrue(overview.waiting.isEmpty)
    }
    func testPromptProvidesExplicitReportAndExitOnlyStopsSession() throws {
        let row = HomeOverview(try HomeOverviewTests().snapshot([[:]])).requests[0]
        let prompt = AgentTerminal.prompt(for:row,taskID:"task-1")
        XCTAssertTrue(prompt.contains("task complete 'task-1'"))
        XCTAssertTrue(prompt.contains("단순 세션 종료는 완료가 아닙니다"))
        let script = AgentTerminal.script(agent:.codex,repository:"/tmp/repo",promptFile:"/tmp/prompt",taskID:"task-1")
        XCTAssertTrue(script.contains("task stop 'task-1'"))
        XCTAssertFalse(script.contains("task complete"))
        XCTAssertTrue(script.contains("codex --yolo"))
    }
}

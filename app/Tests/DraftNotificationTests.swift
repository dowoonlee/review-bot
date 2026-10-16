import XCTest
@testable import ReviewBotApp

final class DraftNotificationTests: XCTestCase {
    func testFirstLaunchAndRestart() throws {
        let draft = try decode(sha: "abc", at: "2026-10-15T10:00:00")
        XCTAssertEqual(DraftPayload.needingNotification([draft], seen: []).count, 1)
        XCTAssertTrue(DraftPayload.needingNotification([draft], seen: [draft.notificationID]).isEmpty)
    }

    func testRegeneratedDraftOnSamePRIsAnnounced() throws {
        let old = try decode(sha: "abc", at: "2026-10-15T10:00:00")
        let regenerated = try decode(sha: "abc", at: "2026-10-15T11:00:00")
        let newCommit = try decode(sha: "def", at: "2026-10-15T11:00:00")
        XCTAssertEqual(old.id, regenerated.id)
        XCTAssertEqual(DraftPayload.needingNotification([regenerated], seen: [old.notificationID]).count, 1)
        XCTAssertEqual(DraftPayload.needingNotification([newCommit], seen: [regenerated.notificationID]).count, 1)
    }

    private func decode(sha: String, at: String) throws -> DraftPayload {
        let data = try JSONSerialization.data(withJSONObject: [
            "repo": "owner/repo", "number": 42, "sha": sha, "at": at
        ])
        return try JSONDecoder().decode(DraftPayload.self, from: data)
    }
}

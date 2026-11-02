import XCTest
@testable import ReviewBotApp

final class UpdateTests: XCTestCase {
    func testOldDraftsAndRunsRemainReadableAndNewOnesCarryVersions() throws {
        let old = #"{"repo":"o/r","number":1,"at":"2026-11-02T17:15:18"}"#
        XCTAssertNil(try JSONDecoder().decode(DraftPayload.self, from: Data(old.utf8)).botVersion)
        let new = #"{"repo":"o/r","number":1,"botVersion":"1.2.0"}"#
        XCTAssertEqual(try JSONDecoder().decode(DraftPayload.self, from: Data(new.utf8)).botVersion, "1.2.0")
        XCTAssertEqual(try JSONDecoder().decode(RunRecord.self, from: Data(new.utf8)).botVersion, "1.2.0")
    }

    func testUpdatePendingAndFailureAreDistinct() throws {
        for phase in ["queued", "downloading", "waiting", "applying", "complete", "failed", "cancelled"] {
            let data = Data("{\"phase\":\"\(phase)\"}".utf8)
            let status = try JSONDecoder().decode(UpdateSnapshot.self, from: data)
            XCTAssertEqual(status.inProgress, ["queued", "downloading", "waiting", "applying"].contains(phase))
        }
    }
}

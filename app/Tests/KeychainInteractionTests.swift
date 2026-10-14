import XCTest
import Security
@testable import ReviewBotApp

final class KeychainInteractionTests: XCTestCase {
    func testBackgroundLookupDisallowsUIAndRestoresExistingPolicy() {
        var original: DarwinBoolean = true
        XCTAssertEqual(SecKeychainGetUserInteractionAllowed(&original), errSecSuccess)
        defer { _ = SecKeychainSetUserInteractionAllowed(original.boolValue) }
        for previous in [true, false] {
            XCTAssertEqual(SecKeychainSetUserInteractionAllowed(previous), errSecSuccess)
            let executed = InsightKeychain.withoutInteraction(blocked: false) {
                var allowed: DarwinBoolean = true
                XCTAssertEqual(SecKeychainGetUserInteractionAllowed(&allowed), errSecSuccess)
                XCTAssertFalse(allowed.boolValue)
                return true
            }
            XCTAssertTrue(executed)
            var restored: DarwinBoolean = true
            XCTAssertEqual(SecKeychainGetUserInteractionAllowed(&restored), errSecSuccess)
            XCTAssertEqual(restored.boolValue, previous)
        }
    }
    func testBackgroundReadNeverTouchesKeychainBeforeExplicitAuthorization() {
        var called = false
        let result = InsightKeychain.backgroundRead(verified: false) {
            called = true
            return InsightKeychain.ReadResult(status: errSecSuccess, data: nil)
        }
        XCTAssertFalse(called)
        XCTAssertEqual(result.status, errSecInteractionNotAllowed)
    }
    func testLockedIsNotMisreportedAsMissing() {
        XCTAssertEqual(InsightKeychain.errorCode(errSecInteractionNotAllowed), "keychain_locked")
        XCTAssertEqual(InsightKeychain.errorCode(errSecAuthFailed), "keychain_locked")
        XCTAssertEqual(InsightKeychain.errorCode(errSecItemNotFound), "missing_key")
    }
    func testBackgroundQueryExplicitlyRefusesAuthenticationUI() {
        let query = InsightKeychain.readQuery(serviceName: "fixture", interactive: false)
        XCTAssertEqual(query[kSecUseAuthenticationUI as String] as? String, kSecUseAuthenticationUIFail as String)
        XCTAssertEqual(query[kSecReturnData as String] as? Bool, true)
        XCTAssertNil(query[kSecValueData as String])
    }
}

import Foundation
import Security
import LocalAuthentication

/// Values go directly to Security.framework; never to command arguments, config, or logs.
enum InsightKeychain {
    static let service = "review-bot-anthropic"
    static let account = "api-key"
    private static let verifiedAccessKey = "anthropicKeychainAccessVerifiedV2"
    static var accessVerified: Bool { accessVerified(for: service) }
    static func accessKey(for serviceName: String) -> String {
        serviceName == service ? verifiedAccessKey : "keychainAccessVerifiedV2." + serviceName
    }
    static func accessVerified(for serviceName: String) -> Bool {
        UserDefaults.standard.bool(forKey: accessKey(for: serviceName))
    }
    static func recordAccess(_ status: OSStatus, serviceName: String = "review-bot-anthropic") {
        UserDefaults.standard.set(status == errSecSuccess, forKey: accessKey(for: serviceName))
    }
    static func backgroundRead(verified: Bool, read: () -> ReadResult) -> ReadResult {
        guard verified else { return ReadResult(status: errSecInteractionNotAllowed, data: nil) }
        return read()
    }
    static func query(serviceName: String = "review-bot-anthropic") -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: serviceName, kSecAttrAccount as String: account]
    }
    private static let interactionLock = NSLock()

    /// LAContext covers data-protection items; legacy login-keychain ACL dialogs also need
    /// the Keychain Services interaction switch. Always restore the caller's process state.
    static func withoutInteraction<T>(blocked: T, _ operation: () -> T) -> T {
        withInteraction(false, blocked: blocked, operation)
    }
    private static func withInteraction<T>(_ allowed: Bool, blocked: T, _ operation: () -> T) -> T {
        interactionLock.lock()
        defer { interactionLock.unlock() }
        var previouslyAllowed: DarwinBoolean = true
        guard SecKeychainGetUserInteractionAllowed(&previouslyAllowed) == errSecSuccess,
              SecKeychainSetUserInteractionAllowed(allowed) == errSecSuccess else { return blocked }
        defer { _ = SecKeychainSetUserInteractionAllowed(previouslyAllowed.boolValue) }
        return operation()
    }
    static func state(serviceName: String = "review-bot-anthropic") -> String {
        guard accessVerified(for: serviceName) else { return "키체인 접근 확인 필요" }
        let result = readResult(serviceName: serviceName, includeData: false)
        return result.status == errSecSuccess ? "등록됨" : result.status == errSecItemNotFound ? "미등록" : "키체인 접근 확인 필요"
    }
    static func errorCode(_ status: OSStatus) -> String {
        status == errSecItemNotFound ? "missing_key" : "keychain_locked"
    }
    static func save(_ value: String, serviceName: String = "review-bot-anthropic") -> OSStatus {
        let data = Data(value.trimmingCharacters(in: .whitespacesAndNewlines).utf8)
        guard !data.isEmpty, data.count <= 8192 else { return errSecParam }
        let updated = SecItemUpdate(query(serviceName: serviceName) as CFDictionary, [kSecValueData as String: data] as CFDictionary)
        if updated != errSecItemNotFound { recordAccess(updated, serviceName: serviceName); return updated }
        var q = query(serviceName: serviceName)
        q[kSecValueData as String] = data
        let added = SecItemAdd(q as CFDictionary, nil)
        recordAccess(added, serviceName: serviceName)
        return added
    }
    struct ReadResult {
        var status: OSStatus
        var data: Data?
    }
    static func readQuery(serviceName: String, interactive: Bool) -> [String: Any] {
        var q = query(serviceName: serviceName)
        q[kSecReturnData as String] = true
        let context = LAContext()
        context.interactionNotAllowed = !interactive
        q[kSecUseAuthenticationContext as String] = context
        // Keep the explicit no-UI key as well: LAContext alone did not prevent legacy dialogs.
        q[kSecUseAuthenticationUI as String] = interactive ? kSecUseAuthenticationUIAllow : kSecUseAuthenticationUIFail
        return q
    }
    static func readResult(serviceName: String = "review-bot-anthropic", interactive: Bool = false,
                           includeData: Bool = true) -> ReadResult {
        withInteraction(interactive, blocked: ReadResult(status: errSecInteractionNotAllowed, data: nil)) {
            var keychain: SecKeychain?
            let copied = SecKeychainCopyDefault(&keychain)
            guard copied == errSecSuccess, let keychain else { return ReadResult(status: copied, data: nil) }
            var keychainStatus: SecKeychainStatus = 0
            let probed = SecKeychainGetStatus(keychain, &keychainStatus)
            guard probed == errSecSuccess else { return ReadResult(status: probed, data: nil) }
            // Don't attempt any item lookup in a locked keychain during background work.
            if !interactive && keychainStatus & UInt32(kSecUnlockStateStatus) == 0 {
                return ReadResult(status: errSecInteractionNotAllowed, data: nil)
            }
            var q = readQuery(serviceName: serviceName, interactive: interactive)
            q[kSecMatchSearchList as String] = [keychain]
            if !includeData {
                q.removeValue(forKey: kSecReturnData as String)
                q[kSecReturnAttributes as String] = true
            }
            var value: CFTypeRef?
            let status = SecItemCopyMatching(q as CFDictionary, &value)
            return ReadResult(status: status, data: status == errSecSuccess && includeData ? value as? Data : nil)
        }
    }
    /// Only a deliberate settings button may request authentication UI. No key leaves this type.
    static func authorize(serviceName: String = "review-bot-anthropic") -> OSStatus {
        let status = readResult(serviceName: serviceName, interactive: true).status
        recordAccess(status, serviceName: serviceName)
        return status
    }
    static func delete(serviceName: String = "review-bot-anthropic") -> OSStatus {
        let status = SecItemDelete(query(serviceName: serviceName) as CFDictionary)
        if status == errSecSuccess || status == errSecItemNotFound { recordAccess(errSecItemNotFound, serviceName: serviceName) }
        return status
    }
}


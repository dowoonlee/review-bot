import SwiftUI
import UserNotifications

// MARK: - 보기 설정

/// 앱 전체에 걸리는 표시 설정. 창을 닫아도 남는다.
@MainActor
@Observable
final class Prefs {
    var fontStep: Int {
        didSet { UserDefaults.standard.set(fontStep, forKey: "fontStep") }
    }
    var pinned: Bool {
        didSet { UserDefaults.standard.set(pinned, forKey: "pinned") }
    }
    var notify: Bool {
        didSet { UserDefaults.standard.set(notify, forKey: "notify") }
    }

    init() {
        let d = UserDefaults.standard
        fontStep = d.object(forKey: "fontStep") as? Int ?? 2      // 0…5, 기본 2
        pinned = d.bool(forKey: "pinned")
        notify = d.object(forKey: "notify") as? Bool ?? true
    }

    // macOS 는 Dynamic Type 을 시스템 폰트에 적용하지 않는다. dynamicTypeSize 를 걸어도
    // .callout·.caption 은 꿈쩍하지 않으므로, 크기를 직접 곱해서 쓴다.
    static let steps: [CGFloat] = [0.85, 0.92, 1.0, 1.12, 1.28, 1.45]
    static let stepLabels = ["아주 작게", "작게", "보통", "크게", "더 크게", "가장 크게"]

    /// macOS 시스템 폰트의 기본 크기. 배율 1.0 이면 손대지 않은 것과 같아야 한다.
    static let baseSize: [Font.TextStyle: CGFloat] = [
        .largeTitle: 26, .title: 22, .title2: 17, .title3: 15,
        .headline: 13, .body: 13, .callout: 12, .subheadline: 11,
        .footnote: 10, .caption: 10, .caption2: 10,
    ]

    var scale: CGFloat { Prefs.steps[min(max(fontStep, 0), Prefs.steps.count - 1)] }
    var stepLabel: String { Prefs.stepLabels[min(max(fontStep, 0), Prefs.steps.count - 1)] }
    var typeSize: DynamicTypeSize { .medium }

    func bigger() { fontStep = min(fontStep + 1, Prefs.steps.count - 1) }
    func smaller() { fontStep = max(fontStep - 1, 0) }
}

// MARK: - 알림

/// 봇이 뭔가 끝냈을 때만 알린다 — 리뷰 요청이 새로 오거나, 초안이 준비됐을 때.
///
/// 알림 센터는 서명·등록이 맞아야 열린다. 로컬에서 ad-hoc 으로 서명한 앱은 거절당하는
/// 일이 흔해서, 실패를 삼키지 않고 상태로 남긴다 — 안 오는 알림을 기다리는 것보다
/// 왜 안 오는지 보이는 편이 낫다. 배너가 막혀도 소리는 나므로 소리는 따로 낸다.
@MainActor
@Observable
final class Notifier: NSObject, UNUserNotificationCenterDelegate {
    static let shared = Notifier()

    enum Status: Equatable {
        case unknown, authorized, denied, failed(String)

        var label: String {
            switch self {
            case .unknown: "확인 중"
            case .authorized: "허용됨"
            case .denied: "거부됨 — 시스템 설정에서 켜세요"
            case .failed(let why): "사용할 수 없음 — \(why)"
            }
        }
        var ok: Bool { self == .authorized }
    }

    private(set) var status: Status = .unknown
    private(set) var lastSent: String?

    /// 알림 센터에 등록을 요청한다.
    ///
    /// 앱이 완전히 뜨기 전에 부르면 `Notifications are not allowed for this application`
    /// 이 돌아오는 일이 있어, 실패하면 잠깐 뒤 몇 번 더 물어본다.
    func prepare(attempt: Int = 1) {
        UNUserNotificationCenter.current().delegate = self
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { ok, err in
            Task { @MainActor in
                if let err {
                    self.status = .failed(err.localizedDescription)
                    NSLog("[review-bot] 알림 권한 요청 %d회차 실패: %@", attempt,
                          err.localizedDescription)
                    if attempt < 4 {
                        try? await Task.sleep(for: .seconds(2))
                        self.prepare(attempt: attempt + 1)
                    }
                } else {
                    self.status = ok ? .authorized : .denied
                    NSLog("[review-bot] 알림 권한 %d회차: %@", attempt, self.status.label)
                }
                self.attempts = attempt
            }
        }
    }

    private(set) var attempts = 0
    var requestedDraftID: String?

    func openDraft(_ id: String) {
        requestedDraftID = id
        AppDelegate.showDashboard?()
        NSApplication.shared.requestUserAttention(.informationalRequest)
    }

    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        completionHandler([.banner, .list])
    }

    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping () -> Void
    ) {
        if response.actionIdentifier == UNNotificationDefaultActionIdentifier,
           let id = response.notification.request.content.userInfo["draftID"] as? String {
            Task { @MainActor in self.openDraft(id) }
        }
        completionHandler()
    }

    func post(title: String, body: String, sound: Bool = true, draftID: String? = nil) {
        lastSent = "\(shortNow()) \(title) — \(body.prefix(60))"
        if sound { NSSound(named: "Glass")?.play() }   // 배너가 막혀도 이건 들린다

        if status.ok {
            let content = UNMutableNotificationContent()
            content.title = title
            content.body = body
            content.sound = sound ? .default : nil
            if let draftID { content.userInfo["draftID"] = draftID }
            UNUserNotificationCenter.current().add(
                UNNotificationRequest(identifier: UUID().uuidString,
                                      content: content, trigger: nil)) { error in
                    guard let error else { return }
                    let message = error.localizedDescription
                    Task { @MainActor in
                        self.status = .failed(message)
                        self.fallback(title: title, body: body)
                        if let draftID { self.openDraft(draftID) }
                    }
                }
        } else {
            fallback(title: title, body: body)
            if let draftID { openDraft(draftID) }
        }
    }

    /// 알림 센터가 막혔을 때의 우회로. 이것도 막혀 있으면 소리만 남는다.
    private func fallback(title: String, body: String) {
        let esc = { (s: String) in s.replacingOccurrences(of: "\"", with: "'") }
        let script = "display notification \"\(esc(body))\" with title \"\(esc(title))\""
        Task.detached {
            let p = Process()
            p.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
            p.arguments = ["-e", script]
            try? p.run()
            p.waitUntilExit()
        }
    }

    private func shortNow() -> String {
        let f = DateFormatter(); f.dateFormat = "HH:mm:ss"
        return f.string(from: Date())
    }
}

// MARK: - 창 다루기

/// SwiftUI 창의 NSWindow 를 꺼내 온다 — 항상 위 고정에 필요하다.
struct WindowAccessor: NSViewRepresentable {
    let onWindow: (NSWindow) -> Void

    func makeNSView(context: Context) -> NSView {
        let view = NSView()
        DispatchQueue.main.async { if let w = view.window { onWindow(w) } }
        return view
    }

    func updateNSView(_ view: NSView, context: Context) {
        DispatchQueue.main.async { if let w = view.window { onWindow(w) } }
    }
}


// MARK: - 배율이 걸린 폰트

/// 앱의 모든 글자는 이 수정자를 지난다. Prefs 가 환경에 없으면 배율 1.0 으로 돈다.
struct AppFont: ViewModifier {
    @Environment(Prefs.self) private var prefs: Prefs?
    let style: Font.TextStyle
    let weight: Font.Weight?
    let mono: Bool
    let digits: Bool

    func body(content: Content) -> some View {
        let size = (Prefs.baseSize[style] ?? 13) * (prefs?.scale ?? 1.0)
        let font = Font.system(size: size, weight: weight, design: mono ? .monospaced : .default)
        return content.font(digits ? font.monospacedDigit() : font)
    }
}

extension View {
    /// `.font(.callout)` 대신 쓴다 — 사용자가 정한 글자 크기를 따른다.
    func f(_ style: Font.TextStyle, _ weight: Font.Weight? = nil,
           mono: Bool = false, digits: Bool = false) -> some View {
        modifier(AppFont(style: style, weight: weight, mono: mono, digits: digits))
    }
}

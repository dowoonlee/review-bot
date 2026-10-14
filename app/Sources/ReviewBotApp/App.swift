import SwiftUI

/// Finder·Launchpad 에서 다시 눌렀을 때 창을 띄운다.
///
/// 메뉴바 전용 앱(LSUIElement)은 Dock 아이콘이 없어서, 창이 안 뜨면 사용자 눈에는
/// "실행이 안 된다" 로 보인다. 이미 떠 있는 앱을 다시 눌러도 macOS 는 새 프로세스를
/// 만들지 않고 이 콜백만 부르므로, 여기서 창을 열어 줘야 한다.
final class AppDelegate: NSObject, NSApplicationDelegate {
    @MainActor static var showDashboard: (() -> Void)?

    func applicationShouldHandleReopen(_ sender: NSApplication,
                                       hasVisibleWindows flag: Bool) -> Bool {
        if !flag { AppDelegate.showDashboard?() }
        return true
    }
}

/// Headless API calls keep the key inside this signed executable.
@main
struct ReviewBotEntry {
    @MainActor static func main() async {
        let args = CommandLine.arguments
        if args.count > 1 && args[1] == "--insight-key-fd" { exit(2) }
        if args.count > 1 && ["--insight-api", "--jev-api"].contains(args[1]) {
            guard args.count == 2 else { exit(2) }
            let result = args[1] == "--jev-api" ? await JevAPI.run() : await InsightAPI.run()
            if let data = try? JSONSerialization.data(withJSONObject: result) {
                try? FileHandle.standardOutput.write(contentsOf: data)
            }
            return
        }
        ReviewBotApp.main()
    }
}

struct ReviewBotApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @State private var store = BotStore()
    @State private var prefs = Prefs()

    var body: some Scene {
        MenuBarExtra {
            MenuBarView()
                .environment(store)
                .environment(prefs)
                .dynamicTypeSize(prefs.typeSize)
                .task { store.start() }
        } label: {
            // 라벨은 앱이 뜨는 순간 만들어지는 유일한 뷰다. 감시를 여기서 시작해야
            // 창을 한 번도 열지 않아도 폴링과 알림이 돈다.
            MenuBarLabel(health: store.health, pending: store.drafts.count,
                         onAppear: { store.start() })
        }
        .menuBarExtraStyle(.window)

        Window("review-bot", id: "dashboard") {
            DashboardView()
                .environment(store)
                .environment(prefs)
                .dynamicTypeSize(prefs.typeSize)
                .background(WindowAccessor { window in
                    window.level = prefs.pinned ? .floating : .normal
                })
                .task { store.start() }
        }
        .defaultSize(width: 1040, height: 660)
        .commands {
            CommandGroup(after: .toolbar) {
                Button("글자 크게") { prefs.bigger() }
                    .keyboardShortcut("+", modifiers: .command)
                Button("글자 작게") { prefs.smaller() }
                    .keyboardShortcut("-", modifiers: .command)
                Divider()
                Toggle("항상 맨 위에 두기", isOn: Binding(
                    get: { prefs.pinned }, set: { prefs.pinned = $0 }))
                    .keyboardShortcut("t", modifiers: [.command, .shift])
            }
        }
    }
}

/// 메뉴바 아이콘. 상태에 따라 모양이 바뀐다 (메뉴바는 단색이라 색으로는 구분되지 않는다).
private struct MenuBarLabel: View {
    let health: BotStore.Health
    let pending: Int
    let onAppear: () -> Void
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        HStack(spacing: 2) {
            Image(systemName: health.symbol)
            // 검토 후 게시 모드의 핵심은 "기다리는 게 있다"는 걸 알아채는 것이다.
            if pending > 0 { Text("\(pending)").f(.caption2, digits: true) }
        }
            .task {
                onAppear()
                let show = {
                    openWindow(id: "dashboard")
                    NSApplication.shared.activate()
                }
                AppDelegate.showDashboard = show
                // 실행했는데 아무것도 안 보이면 안 켜진 것과 구별되지 않는다. 창을 띄운다.
                show()
            }
    }
}

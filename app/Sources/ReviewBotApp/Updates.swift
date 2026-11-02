import AppKit
import SwiftUI

struct UpdateSnapshot: Decodable, Sendable {
    struct Previous: Decodable, Sendable { var root: String }
    struct Release: Decodable, Sendable {
        var version: String
        var url: String
        var notes: String
    }
    var latest: Release?
    var available: Bool?
    var checkedAt: Double?
    var checkError: String?
    var phase: String?
    var message: String?
    var previous: Previous?
    var inProgress: Bool { ["queued", "downloading", "waiting", "applying"].contains(phase ?? "") }
}

extension BotStore {
    var installedBotVersion: String {
        let pointer = BotPaths.root.appending(path: ".review-bot-current/VERSION")
        let fallback = BotPaths.root.appending(path: "VERSION")
        return ((try? String(contentsOf: pointer, encoding: .utf8))
                ?? (try? String(contentsOf: fallback, encoding: .utf8))
                ?? "버전 기록 없음").trimmingCharacters(in: .whitespacesAndNewlines)
    }

    func readUpdates() {
        if let data = try? Data(contentsOf: BotPaths.state.appending(path: "update.json")) {
            updateSnapshot = try? JSONDecoder().decode(UpdateSnapshot.self, from: data)
        }
    }

    func checkUpdates() async {
        guard !checkingUpdates else { return }
        checkingUpdates = true
        lastUpdateCheckAttempt = Date()
        defer { checkingUpdates = false }
        let result = await Shell.run([BotPaths.executable.path, "update", "--check", "--json"], cwd: BotPaths.root.path)
        readUpdates()
        updateError = result.code == 0 ? nil : updateSnapshot?.checkError ?? result.output
    }

    func requestUpdate(rollback: Bool = false) async {
        guard busy == nil else { return }
        busy = rollback ? "이전 버전 복구" : "업데이트 예약"
        defer { busy = nil }
        var args = [BotPaths.executable.path, "update", "--json"]
        if rollback { args.append("--rollback") }
        else if let version = updateSnapshot?.latest?.version { args += ["--version", version] }
        if Bundle.main.bundleURL.pathExtension == "app" {
            args += ["--app-path", Bundle.main.bundleURL.path]
        }
        let result = await Shell.run(args, cwd: BotPaths.root.path)
        if result.code != 0 {
            struct Failure: Decodable { let error: String }
            updateError = (try? JSONDecoder().decode(Failure.self, from: Data(result.output.utf8)))?.error ?? result.output
        } else { updateError = nil }
        readUpdates()
    }

    func cancelUpdate() async {
        let result = await Shell.run([BotPaths.executable.path, "update", "--cancel", "--json"], cwd: BotPaths.root.path)
        if result.code != 0 { updateError = result.output }
        readUpdates()
    }
}

struct UpdateSettingsView: View {
    @Environment(BotStore.self) private var store
    @State private var confirmingRollback = false

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label("업데이트", systemImage: "arrow.down.app").f(.headline)
            Text("앱 \(store.appVersion) · 봇 \(store.installedBotVersion)")
                .f(.callout).foregroundStyle(.secondary)
            if let release = store.updateSnapshot?.latest {
                HStack {
                    Text("최신 릴리스 \(release.version)").f(.callout)
                    if let url = URL(string: release.url), url.scheme == "https", url.host == "github.com" {
                        Link("변경 사항", destination: url)
                    }
                }
                if store.updateSnapshot?.available == true {
                    Text(release.notes).f(.caption).lineLimit(6).textSelection(.enabled)
                }
            }
            if let message = store.updateSnapshot?.message {
                Text(message).f(.callout).foregroundStyle(.secondary)
            }
            if let error = store.updateError ?? store.updateSnapshot?.checkError {
                Text(error).f(.callout).foregroundStyle(.red).textSelection(.enabled)
            }
            HStack {
                Button(store.checkingUpdates ? "확인 중…" : "업데이트 확인") {
                    Task { await store.checkUpdates() }
                }.disabled(store.checkingUpdates)
                if store.updateSnapshot?.inProgress == true {
                    ProgressView().controlSize(.small)
                    Button("예약 취소") { Task { await store.cancelUpdate() } }
                        .disabled(store.updateSnapshot?.phase == "applying")
                } else {
                    Button("업데이트 예약") { Task { await store.requestUpdate() } }
                        .disabled(store.updateSnapshot?.available != true || store.busy != nil || store.checkingUpdates)
                    Button("이전 버전으로 복구") { confirmingRollback = true }
                        .disabled(store.busy != nil || store.updateSnapshot?.previous == nil)
                }
            }
            if store.appVersion != store.installedBotVersion && store.updateSnapshot?.phase == "complete" {
                Button("앱 다시 시작") {
                    let url = Bundle.main.bundleURL
                    Task {
                        let result = await Shell.detach(["/bin/sh", "-c", "sleep 1; /usr/bin/open -n \"$1\"", "restart", url.path])
                        if result.code == 0 { NSApplication.shared.terminate(nil) }
                    }
                }
            }
            Text("현재 PR을 마친 뒤 적용합니다. 설정·초안·사용자 리뷰어 정의는 유지하며, 로컬 수정이 있으면 중단합니다.")
                .f(.caption).foregroundStyle(.secondary)
        }
        .confirmationDialog("이전 버전으로 되돌리시겠습니까?", isPresented: $confirmingRollback) {
            Button("이전 버전으로 복구") { Task { await store.requestUpdate(rollback: true) } }
        } message: { Text("현재 PR을 마친 뒤 앱과 봇을 이전 버전으로 되돌립니다. 리뷰 기록과 설정은 유지합니다.") }
    }
}

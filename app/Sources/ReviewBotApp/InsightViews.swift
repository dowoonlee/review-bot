import AppKit
import SwiftUI
import Security
import LocalAuthentication

struct RequestInsight: Decodable, Sendable {
    var selection: JevDecision?
    var nextAction: String
    var status: String
    var summary: String?
    var items: [InsightItem]?
    var analyzedAt: String?
    var error: String?
    var incomplete: Bool?
    var inputTokens: Int?
    var outputTokens: Int?
}

struct InsightItem: Decodable, Sendable {
    struct Source: Decodable, Sendable { var id: String; var url: String }
    var kind: String
    var text: String
    var sources: [Source]
    var replyDraft: String
    var label: String { kind == "change" ? "수정 검토" : kind == "question" ? "답변 검토" : "후속 확인" }
}

struct InsightSettings: Decodable, Sendable {
    var enabled: Bool
    var model: String
    var maxPerTick: Int
    var workspaceID: String?
}

struct InsightDetailView: View {
    let insight: RequestInsight
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label(insight.nextAction, systemImage: "arrow.right.circle").f(.headline)
            if insight.status == "disabled" {
                Text("설정에서 Claude 요청 분석을 켜면 리뷰 내용과 대응 항목을 정리합니다.")
                    .f(.caption).foregroundStyle(.secondary)
            } else {
                if insight.status != "ready" {
                    Text(insight.error ?? (insight.status == "stale" ? "새 변경 감지 · 이전 분석입니다" : "다음 틱에서 분석할 예정입니다"))
                        .f(.caption).foregroundStyle(.orange)
                    if insight.summary != nil { Text("아래 내용은 마지막 성공 분석입니다.").f(.caption) }
                }
                if let summary = insight.summary { Text(summary).f(.callout).textSelection(.enabled) }
                if insight.incomplete == true {
                    Text("대화 일부만 분석했습니다. 전체 내용은 GitHub에서 확인하세요.")
                        .f(.caption).foregroundStyle(.orange)
                }
                ForEach(Array((insight.items ?? []).enumerated()), id: \.offset) { _, item in
                    VStack(alignment: .leading, spacing: 6) {
                        Text(item.label).f(.caption, .semibold).foregroundStyle(.secondary)
                        Text(item.text).f(.callout).textSelection(.enabled)
                        HStack {
                            ForEach(Array(item.sources.enumerated()), id: \.offset) { index, source in
                                if let url = URL(string: source.url), url.scheme == "https", url.host == "github.com" {
                                    Link("근거 \(index + 1)", destination: url).f(.caption)
                                }
                            }
                        }
                        if !item.replyDraft.isEmpty {
                            DisclosureGroup("답변 초안") {
                                VStack(alignment: .leading, spacing: 6) {
                                    Text(item.replyDraft).f(.callout).textSelection(.enabled)
                                    Button("초안 복사") {
                                        NSPasteboard.general.clearContents()
                                        NSPasteboard.general.setString(item.replyDraft, forType: .string)
                                    }.buttonStyle(.bordered)
                                }.padding(.top, 6)
                            }.f(.caption)
                        }
                    }.padding(10).frame(maxWidth: .infinity, alignment: .leading)
                        .background(.quaternary.opacity(0.35), in: RoundedRectangle(cornerRadius: 8))
                }
                if let at = insight.analyzedAt {
                    Text("Claude 분석 · \(at) · 입력 \(insight.inputTokens ?? 0) / 출력 \(insight.outputTokens ?? 0) 토큰")
                        .f(.caption2).foregroundStyle(.secondary)
                }
            }
        }.frame(maxWidth: .infinity, alignment: .leading)
    }
}


struct InsightSettingsView: View {
    @Environment(BotStore.self) private var store
    @State private var apiKey = ""
    @State private var keyState = "확인 중"
    @State private var message = ""
    @State private var model = ""
    @State private var workspaceID = ""
    @State private var authorizing = false
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Claude 요청 분석").f(.headline)
            Text("감시 저장소의 받은·보낸 PR 대화를 Claude API로 보내 요약과 다음 대응을 정리합니다. 변경된 내용만 분석하며 API 사용량이 별도로 발생합니다.")
                .f(.caption).foregroundStyle(.secondary)
            Toggle("틱마다 요청 분석", isOn: Binding(
                get: { store.status?.insightSettings?.enabled ?? false },
                set: { on in Task { await store.setInsights(on) } }))
                .disabled(store.busy != nil)
            HStack {
                SecureField("Anthropic API 키", text: $apiKey).textFieldStyle(.roundedBorder)
                Button("키 저장") {
                    let result = InsightKeychain.save(apiKey)
                    apiKey = ""
                    keyState = InsightKeychain.state()
                    message = result == errSecSuccess ? "키체인에 저장했습니다." : "저장 실패 (\(result))"
                }.disabled(authorizing || apiKey.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                Button("키 삭제", role: .destructive) {
                    let result = InsightKeychain.delete()
                    apiKey = ""
                    keyState = InsightKeychain.state()
                    message = result == errSecSuccess || result == errSecItemNotFound ? "키를 삭제했습니다." : "삭제 실패 (\(result))"
                }
            }
            HStack {
                Text("API 키: \(keyState) · macOS 키체인에 보관").f(.caption).foregroundStyle(.secondary)
                Button("키체인 접근 확인") {
                    authorizing = true
                    Task {
                        let result = await Task.detached { InsightKeychain.authorize() }.value
                        message = result == errSecSuccess ? "키체인 접근을 확인했습니다." : "접근이 허용되지 않았습니다 (\(result)). 다음 자동 실행에서도 암호 창을 열지 않습니다."
                        keyState = InsightKeychain.state()
                        authorizing = false
                    }
                }.disabled(authorizing)
                if authorizing { ProgressView().controlSize(.small) }
                Button("연결 확인") { Task { await store.checkInsightConnection() } }
                    .disabled(store.busy != nil)
            }
            Text("자동 분석은 암호 창을 열지 않습니다. 접근이 막혔을 때만 ‘키체인 접근 확인’을 눌러 직접 허용해 주세요.")
                .f(.caption).foregroundStyle(.secondary)
            if !store.lastCommandOutput.isEmpty {
                Text(store.lastCommandOutput).f(.caption).foregroundStyle(.secondary)
            }
            HStack {
                TextField("API 모델 ID", text: $model).textFieldStyle(.roundedBorder)
                Button("모델 적용") { Task { await store.setInsightModel(model) } }
                    .disabled(store.busy != nil || !model.hasPrefix("claude-"))
            }
            HStack {
                TextField("Workspace ID (선택, wrkspc_…)", text: $workspaceID).textFieldStyle(.roundedBorder)
                Button("Workspace 적용") { Task { await store.setInsightWorkspace(workspaceID) } }
                    .disabled(store.busy != nil || !InsightAPI.validWorkspace(workspaceID))
            }
            Text("특정 워크스페이스에 속한 키는 생략할 수 있습니다. 여러 워크스페이스에 접근하는 키는 대상 ID를 지정하세요.")
                .f(.caption).foregroundStyle(.secondary)
            Text("틱당 최대 \(store.status?.insightSettings?.maxPerTick ?? 3)개 PR · 답변 초안은 상세 화면에서 복사할 수 있습니다.")
                .f(.caption).foregroundStyle(.secondary)
            if !message.isEmpty { Text(message).f(.caption) }
        }
        .onAppear { keyState = InsightKeychain.state() }
        .task(id: store.status?.insightSettings?.model) { model = store.status?.insightSettings?.model ?? "claude-sonnet-5-5" }
        .task(id: store.status?.insightSettings?.workspaceID) { workspaceID = store.status?.insightSettings?.workspaceID ?? "" }
        .onDisappear { apiKey = "" }
    }
}

import AppKit
import SwiftUI

struct SessionLinkView: View {
    @Environment(BotStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    let row: HomeRequest
    @State private var sessionID = ""
    @State private var sessions: [ExistingAgentSession.Session] = []
    @State private var terminalID = ""
    @State private var recommendation: SessionRecommendation?
    @State private var recommending = false
    @State private var loading = false
    @State private var saving = false
    @State private var error: String?

    private var selected: ExistingAgentSession.Session? { sessions.first { $0.id == terminalID } }
    private var normalizedID: String? {
        UUID(uuidString: sessionID.trimmingCharacters(in: .whitespacesAndNewlines))?.uuidString.lowercased()
    }
    private func title(_ session: ExistingAgentSession.Session) -> String {
        "\(session.agent?.label ?? "CLI") · \(session.name) · \(session.branch ?? session.tty)"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("기존 세션 연결").font(.headline)
            Text("\(row.repo) #\(row.number) · \(row.title)").lineLimit(2)
            Picker("전달할 iTerm 탭", selection: $terminalID) {
                Text("탭을 선택해 주세요").tag("")
                ForEach(sessions) { session in
                    Text(title(session)).tag(session.id)
                }
            }.disabled(saving || loading)
            if let recommendation, recommendation.terminalID == terminalID {
                Label(recommendation.label, systemImage: "sparkles").font(.caption).foregroundStyle(Color.accentColor)
            } else if recommending {
                HStack(spacing: 6) {
                    ProgressView().controlSize(.mini)
                    Text("Jev가 이 PR을 작업 중인 탭을 찾고 있습니다.").font(.caption).foregroundStyle(.secondary)
                }
            }
            if let selected, let directory = selected.directory {
                Text(directory + (selected.repo.map { " · \($0)" } ?? "")).font(.caption2).foregroundStyle(.secondary).lineLimit(1)
            }
            HStack {
                Button("탭 새로고침") { Task { await refresh() } }.disabled(saving || loading)
                if loading { ProgressView().controlSize(.small) }
            }
            if !loading && sessions.isEmpty {
                Text("실행 중인 Codex·Claude 탭이 없습니다.").foregroundStyle(.secondary)
            }
            TextField("대화 세션 ID (선택 사항, UUID)", text: $sessionID)
                .textFieldStyle(.roundedBorder).disabled(saving)
            Text("선택한 탭에 작업 안내를 붙여넣고 Enter를 보냅니다. CLI의 입력창을 비우고 일반 대화 입력 상태로 준비해 주세요.")
                .font(.callout).foregroundStyle(.secondary)
            if store.homeTasks.currentTask(for: row.id) != nil {
                Text("다른 세션을 연결하면 현재 작업 기록을 대체합니다.").font(.caption).foregroundStyle(.orange)
            }
            if let error { Text(error).foregroundStyle(.red).textSelection(.enabled) }
            HStack {
                Spacer()
                Button("취소") { dismiss() }.keyboardShortcut(.cancelAction).disabled(saving)
                Button("연결하고 작업 안내 전송") {
                    guard let selectedAgent = selected?.agent else { return }
                    let selectedTerminal = terminalID
                    let conversationID = normalizedID
                    saving = true
                    error = nil
                    Task {
                        do {
                            guard try await ExistingAgentSession.available(selectedAgent).contains(where: { $0.id == selectedTerminal }) else {
                                throw HomeTaskClient.Failure(message: "선택한 CLI 탭을 찾지 못했습니다. 새로고침 후 다시 선택해 주세요.")
                            }
                            let task = try await HomeTaskClient.start(row, agent: selectedAgent.rawValue,
                                sessionID: conversationID, terminalID: selectedTerminal)
                            store.readHomeTasks()
                            try await ExistingAgentSession.send(selectedAgent, terminalID: selectedTerminal,
                                prompt: AgentTerminal.prompt(for: row, taskID: task.id))
                            dismiss()
                        } catch {
                            self.error = error.localizedDescription
                        }
                        saving = false
                    }
                }.keyboardShortcut(.defaultAction).disabled(saving || loading || selected?.agent == nil || (!sessionID.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && normalizedID == nil))
            }
        }.padding(24).frame(width: 560)
            .task { await refresh() }
            .interactiveDismissDisabled(saving)
    }

    private func refresh() async {
        loading = true
        error = nil
        recommendation = nil
        let found: [ExistingAgentSession.Session]
        do {
            found = try await ExistingAgentSession.availableAll()
        } catch {
            guard !Task.isCancelled else { return }
            sessions = []
            terminalID = ""
            loading = false
            self.error = error.localizedDescription
            return
        }
        guard !Task.isCancelled else { return }
        sessions = found
        if !found.contains(where: { $0.id == terminalID }) { terminalID = "" }
        loading = false
        await recommend(found)
    }

    /// 사용자가 이미 고른 탭은 바꾸지 않는다. 첫 조회처럼 비어 있을 때만 추천을 선택해 둔다.
    private func apply(_ pick: SessionRecommendation) {
        recommendation = pick
        if terminalID.isEmpty { terminalID = pick.terminalID }
    }

    private func recommend(_ found: [ExistingAgentSession.Session]) async {
        if let pick = SessionRecommender.deterministic(row: row, sessions: found, tasks: store.homeTasks) {
            apply(pick)
            return
        }
        guard !found.isEmpty, JevRegistration.enabled else { return }
        recommending = true
        let labels = SessionRecommender.labels(for: found)
        let input = JevAPI.Input(action: "match-session", model: JevRegistration.model, registration: JevRegistration.generation,
                                 context: SessionRecommender.context(row: row, sessions: found, labels: labels), choices: labels)
        let output = await JevAPI.run(input: input)
        guard !Task.isCancelled, sessions.map(\.id) == found.map(\.id) else { recommending = false; return }
        if let pick = SessionRecommender.recommendation(from: output, sessions: found, labels: labels) { apply(pick) }
        recommending = false
    }
}

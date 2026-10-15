import AppKit
import SwiftUI

struct SessionLinkView: View {
    @Environment(BotStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    let row: HomeRequest
    @State private var agent: AgentTerminal.Agent = .codex
    @State private var sessionID = ""
    @State private var sessions: [ExistingAgentSession.Session] = []
    @State private var terminalID = ""
    @State private var loading = false
    @State private var saving = false
    @State private var error: String?

    private var normalizedID: String? {
        UUID(uuidString: sessionID.trimmingCharacters(in: .whitespacesAndNewlines))?.uuidString.lowercased()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("기존 세션 연결").font(.headline)
            Text("\(row.repo) #\(row.number) · \(row.title)").lineLimit(2)
            Picker("에이전트", selection: $agent) {
                ForEach(AgentTerminal.Agent.allCases, id: \.self) { value in
                    Text(value.label).tag(value)
                }
            }.pickerStyle(.segmented).disabled(saving)
            Picker("전달할 iTerm 탭", selection: $terminalID) {
                Text("탭을 선택해 주세요").tag("")
                ForEach(sessions) { session in
                    Text("\(session.name) · \(session.tty)").tag(session.id)
                }
            }.disabled(saving || loading)
            HStack {
                Button("탭 새로고침") { Task { await refresh() } }.disabled(saving || loading)
                if loading { ProgressView().controlSize(.small) }
            }
            if !loading && sessions.isEmpty {
                Text("실행 중인 \(agent.label) 탭이 없습니다.").foregroundStyle(.secondary)
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
                    let selectedTerminal = terminalID
                    let selectedAgent = agent
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
                }.keyboardShortcut(.defaultAction).disabled(saving || loading || terminalID.isEmpty || (!sessionID.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && normalizedID == nil))
            }
        }.padding(24).frame(width: 560)
            .task(id: agent) { await refresh() }
            .interactiveDismissDisabled(saving)
    }
    private func refresh() async {
        loading = true
        error = nil
        let selectedAgent = agent
        do {
            let found = try await ExistingAgentSession.available(selectedAgent)
            guard !Task.isCancelled, agent == selectedAgent else { return }
            sessions = found
            if !sessions.contains(where: { $0.id == terminalID }) { terminalID = "" }
        } catch {
            guard !Task.isCancelled, agent == selectedAgent else { return }
            sessions = []
            terminalID = ""
            self.error = error.localizedDescription
        }
        loading = false
    }
}

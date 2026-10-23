import SwiftUI

struct HomeBriefingView: View {
    @Environment(BotStore.self) private var store
    @Binding var selection: Selection?
    @State private var showAllActions = false
    @State private var showAllChanges = false
    @State private var showAllCompleted = false
    @State private var sessionLink: HomeRequest?
    @State private var launching: String?
    @State private var launchMessages: [String: String] = [:]

    private var overview: HomeOverview { store.homeOverview }
    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 5) {
                    Text("나의 리뷰 현황").f(.title2, .semibold)
                    if store.status == nil {
                        Text("리뷰 요청을 불러오고 있습니다.").f(.callout).foregroundStyle(.secondary)
                    } else {
                        Text("확인할 PR \(overview.actions.count)건, 대기 중 \(overview.waiting.count)건")
                            .f(.callout)
                        Text("보낸 요청 \(store.sentPRs.count)건 · 받은 요청 \(store.status?.pending.count ?? 0)건")
                            .f(.caption).foregroundStyle(.secondary)
                    }
                }
                Spacer()
                Button { Task { await store.refreshStatus() } } label: {
                    Label("새로고침", systemImage: "arrow.clockwise")
                }.disabled(store.isRefreshingStatus)
            }
            if !store.homeSnapshotComplete && store.status != nil {
                Label("일부 조회가 실패했습니다. 아래 내용은 확인 가능한 범위의 상태입니다.", systemImage: "exclamationmark.triangle")
                    .f(.caption).foregroundStyle(.orange)
            }
            analysisStatus
            HStack {
                Text(store.status?.selectionSettings?.enabled == true ? "Jev 선택·분류 ON · 대응 주체와 같은 등급 내 우선순위 평가" : "Jev 선택·분류 OFF · 모델 등록 시 자동 활성화")
                    .f(.caption).foregroundStyle(.secondary)
                Spacer()
                Button("분류 모델 설정") { selection = .settings }
            }
            Divider()
            HStack {
                Text("지금 할 일").f(.headline)
                Spacer()
                Text("충돌·CI 실패와 수정 요청부터 표시").f(.caption2).foregroundStyle(.secondary)
            }
            if overview.actions.isEmpty {
                Text(store.status == nil ? "조회 후 표시합니다." : "현재 확인된 상태에서 바로 대응할 항목이 없습니다.")
                    .f(.callout).foregroundStyle(.secondary)
            }
            ForEach(Array((showAllActions ? overview.actions : Array(overview.actions.prefix(3))).enumerated()), id: \.element.id) { index, row in
                actionRow(row, index: index)
                if index < min(overview.actions.count, showAllActions ? overview.actions.count : 3) - 1 { Divider() }
            }
            if overview.actions.count > 3 {
                Button(showAllActions ? "접기" : "할 일 \(overview.actions.count)건 모두 보기") { showAllActions.toggle() }
                    .f(.caption)
            }
            if !overview.waiting.isEmpty {
                DisclosureGroup("대기 중 \(overview.waiting.count)건") {
                    VStack(alignment: .leading, spacing: 8) {
                        ForEach(overview.waiting) { row in
                            Button { selection = row.selection } label: {
                                HStack {
                                    Text("#\(String(row.number))").f(.callout, .semibold)
                                    Text(row.action).f(.callout)
                                    if row.insight?.selection?.usable == true { Text("Jev").f(.caption2).foregroundStyle(Color.accentColor) }
                                    Spacer()
                                    Text(row.repo).f(.caption).foregroundStyle(.secondary)
                                }.contentShape(Rectangle())
                            }.buttonStyle(.plain)
                        }
                    }.padding(.top, 8)
                }.f(.callout)
            }
            if !store.homeTasks.completed.isEmpty {
                Divider()
                HStack {
                    Text("최근 완료 \(store.homeTasks.completed.count)건").f(.headline)
                    Spacer()
                    Text("새 요청이 생기면 다시 표시됩니다").f(.caption2).foregroundStyle(.secondary)
                }
                ForEach(showAllCompleted ? store.homeTasks.completed : Array(store.homeTasks.completed.prefix(5))) { task in
                    HStack(alignment: .top) {
                        Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
                        VStack(alignment: .leading, spacing: 4) {
                            Text("#\(String(task.number))  \(task.label)").f(.callout, .semibold)
                            Text(task.title).f(.caption).foregroundStyle(.secondary).lineLimit(1)
                            Text("\(task.repo) · \(task.completedAt ?? "")").f(.caption2).foregroundStyle(.secondary)
                            if let error = task.lastSyncError { Text(error).f(.caption).foregroundStyle(.orange) }
                        }
                        Spacer()
                        Button("다시 열기") { Task { await store.reopenHomeTask(task) } }
                            .disabled(!overview.requests.contains(where: { $0.id == task.requestID }))
                            .help("현재 요청 목록에 있는 PR을 다시 할 일로 표시합니다")
                    }
                }
            }
            if store.homeTasks.completed.count > 5 {
                Button(showAllCompleted ? "완료 항목 접기" : "완료 항목 모두 보기") { showAllCompleted.toggle() }.f(.caption)
            }
            if let error = store.homeTaskError { Text(error).f(.caption).foregroundStyle(.orange) }
            if let error = store.sessionCheckError { Text(error).f(.caption).foregroundStyle(.orange) }
            Divider()
            HStack {
                VStack(alignment: .leading, spacing: 3) {
                    Text("지난 확인 이후 변화").f(.headline)
                    if let at = store.homeAcknowledgedAt {
                        Text("\(at.formatted(date: .abbreviated, time: .shortened)) 이후")
                            .f(.caption2).foregroundStyle(.secondary)
                    }
                }
                Spacer()
                Button("여기까지 확인") { store.acknowledgeHome() }
                    .disabled(!store.homeSnapshotComplete || store.homeChanges.isEmpty)
            }
            if store.homeChanges.isEmpty {
                Text(store.homeBaseline == nil ? "첫 조회가 완료되면 변경 비교를 시작합니다." : "새로 확인된 변화가 없습니다.")
                    .f(.caption).foregroundStyle(.secondary)
            }
            ForEach(showAllChanges ? store.homeChanges : Array(store.homeChanges.prefix(4))) { change in
                Button { selection = change.request.selection } label: {
                    HStack(alignment: .top) {
                        Text("#\(String(change.request.number))").f(.callout, .semibold)
                        VStack(alignment: .leading, spacing: 3) {
                            Text(change.text).f(.callout)
                            Text(change.request.repo).f(.caption2).foregroundStyle(.secondary)
                        }
                        Spacer()
                        Image(systemName: "chevron.right").f(.caption).foregroundStyle(.secondary)
                    }.contentShape(Rectangle())
                }.buttonStyle(.plain)
            }
            if store.homeChanges.count > 4 {
                Button(showAllChanges ? "변화 접기" : "변화 \(store.homeChanges.count)건 모두 보기") { showAllChanges.toggle() }.f(.caption)
            }
        }
        .sheet(item: $sessionLink) { selected in
            SessionLinkView(row: selected)
        }
        .padding(18)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.quaternary.opacity(0.22), in: RoundedRectangle(cornerRadius: 10))
    }

    private var analysisStatus: some View {
        VStack(alignment: .leading, spacing: 6) {
            if store.status?.insightSettings?.enabled != true {
                HStack {
                    Label("Claude 분석 꺼짐", systemImage: "sparkles").f(.callout)
                    Spacer()
                    Button("분석 설정") { selection = .settings }
                }
                Text("현재는 GitHub 상태에 따른 안내입니다. 분석을 켜면 수정·답변할 내용을 홈에 정리합니다.")
                    .f(.caption).foregroundStyle(.secondary)
            } else {
                Label("Claude 분석 완료 \(overview.ready)건 · 대기·갱신 필요 \(overview.pending)건 · 실패 \(overview.failures)건", systemImage: "sparkles")
                    .f(.caption)
                if let at = overview.lastAnalysis {
                    Text("마지막 성공 분석 \(at)").f(.caption2).foregroundStyle(.secondary)
                } else {
                    Text(overview.ready == 0 ? "아직 완료된 분석이 없습니다. 다음 틱에서 순서대로 분석합니다." : "분석 시각을 확인할 수 없습니다.").f(.caption).foregroundStyle(.secondary)
                }
                if overview.excluded > 0 {
                    Text("감시 대상이 아닌 요청 \(overview.excluded)건은 상태만 표시합니다.").f(.caption2).foregroundStyle(.secondary)
                }
                if overview.incomplete > 0 {
                    Text("\(overview.incomplete)건은 대화 일부만 분석했습니다.").f(.caption).foregroundStyle(.orange)
                }
                if let failed = overview.requests.first(where: { ["error", "missing_key"].contains($0.insight?.status ?? "") }) {
                    HStack {
                        Text(failed.insight?.error ?? "분석을 갱신하지 못했습니다.").f(.caption).foregroundStyle(.orange)
                        Spacer()
                        Button("분석 설정") { selection = .settings }
                    }
                }
            }
        }
    }

    private func actionRow(_ row: HomeRequest, index: Int) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            Button { selection = row.selection } label: {
                HStack(alignment: .top, spacing: 10) {
                    Text(String(index + 1)).f(.title3, .semibold, digits: true)
                        .foregroundStyle(row.priority <= 1 ? Color.orange : Color.secondary)
                        .frame(width: 22, alignment: .leading)
                    VStack(alignment: .leading, spacing: 4) {
                        Text("#\(String(row.number))  \(row.action)").f(.callout, .semibold)
                        Text(row.title).f(.caption).foregroundStyle(.secondary).lineLimit(2)
                        Text("\(row.repo) · \(row.direction)").f(.caption2).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Image(systemName: "chevron.right").f(.caption).foregroundStyle(.secondary)
                }.contentShape(Rectangle())
            }.buttonStyle(.plain)
            HStack(alignment: .top) {
                Text(row.reason).f(.callout).textSelection(.enabled)
                if row.currentAI, let item = row.insight?.items?.first(where: { $0.text == row.reason }),
                   let source = item.sources.first, let url = URL(string: source.url), url.scheme == "https", url.host == "github.com" {
                    Link("근거", destination: url).f(.caption)
                }
            }
            if row.currentAI, let summary = row.insight?.summary, !summary.isEmpty, summary != row.reason {
                Text(summary).f(.caption).foregroundStyle(.secondary).textSelection(.enabled)
            }
            if row.currentAI {
                ForEach(Array((row.insight?.items ?? []).filter { $0.text != row.reason }.prefix(2).enumerated()), id: \.offset) { _, item in
                    HStack(alignment: .top) {
                        Text("• " + item.text).f(.caption).textSelection(.enabled)
                        if let source = item.sources.first, let url = URL(string: source.url), url.scheme == "https", url.host == "github.com" {
                            Link("근거", destination: url).f(.caption)
                        }
                    }
                }
            }
            if let task = store.homeTasks.currentTask(for: row.id) {
                if let sessionID = task.sessionID ?? task.terminalID {
                    let alive = store.sessionLiveness[task.id]
                    HStack {
                        Label("연결된 세션 · \(task.agent) · \(sessionID)", systemImage: "link")
                            .f(.caption).textSelection(.enabled)
                        if let alive {
                            Text(alive ? "연결됨" : "세션 종료").f(.caption2)
                                .padding(.horizontal, 6).padding(.vertical, 2)
                                .background((alive ? Color.green : Color.red).opacity(0.15), in: Capsule())
                                .foregroundStyle(alive ? Color.green : Color.red)
                                .help(alive ? "새로고침 때 iTerm 탭에서 \(task.agent) 가 실행 중인 것을 확인했습니다."
                                            : "새로고침 때 iTerm 탭이 닫혔거나 \(task.agent) 가 종료된 것을 확인했습니다. 기존 세션 연결로 다시 연결하세요.")
                        }
                        if let terminalID = task.terminalID {
                            Button {
                                launching = row.id
                                Task {
                                    do {
                                        try await ExistingAgentSession.focus(terminalID: terminalID)
                                        launchMessages[row.id] = nil
                                    } catch { launchMessages[row.id] = error.localizedDescription }
                                    launching = nil
                                }
                            } label: {
                                Label("세션으로 이동", systemImage: "arrow.up.forward.app")
                            }.f(.caption).disabled(launching != nil || alive == false)
                                .help("연결한 iTerm 창·탭·분할 화면으로 이동합니다")
                        }
                        if let terminalID = task.terminalID, let agent = AgentTerminal.Agent(rawValue: task.agent) {
                            Button("작업 안내 전송") {
                                launching = row.id
                                Task {
                                    do {
                                        try await ExistingAgentSession.send(agent, terminalID: terminalID,
                                            prompt: AgentTerminal.prompt(for: row, taskID: task.id))
                                        launchMessages[row.id] = "연결한 iTerm 탭에 작업 안내를 전송했습니다."
                                    } catch { launchMessages[row.id] = error.localizedDescription }
                                    launching = nil
                                }
                            }.f(.caption).disabled(launching != nil || alive == false)
                        }
                        Button("작업 안내 복사") {
                            NSPasteboard.general.clearContents()
                            NSPasteboard.general.setString(AgentTerminal.prompt(for: row, taskID: task.id), forType: .string)
                            launchMessages[row.id] = "연결한 대화에 작업 안내를 붙여넣어 주세요."
                        }.f(.caption)
                    }
                }

                if task.status == "running" {
                    Label("작업 중 · \(task.agent)", systemImage: "terminal").f(.caption).foregroundStyle(.orange)
                    Button("작업 중 표시 해제") { Task { await store.reopenHomeTask(task) } }.f(.caption)
                } else if task.status == "stopped" {
                    Text("세션 종료 · 완료 보고 없음").f(.caption).foregroundStyle(.secondary)
                } else if task.status == "reopened" {
                    Text(task.reopenReason ?? "다시 열린 작업").f(.caption).foregroundStyle(.orange)
                }
            }
            if let decision = row.insight?.selection {
                Text(decision.label).f(.caption).foregroundStyle(decision.usable ? Color.accentColor : Color.secondary)
            }
            HStack(spacing: 8) {
                ForEach(AgentTerminal.Agent.allCases, id: \.rawValue) { agent in
                    Button {
                        let path = store.status?.repoDetails?.first(where: { $0.slug == row.repo })?.localPath ?? ""
                        launching = row.id
                        Task {
                            launchMessages[row.id] = await AgentTerminal.launch(agent, row: row, repository: path)
                            store.readHomeTasks()
                            launching = nil
                        }
                    } label: {
                        Label("\(agent.label)로 열기", systemImage: "terminal")
                    }
                    .buttonStyle(.bordered)
                    .disabled(launching != nil)
                    .help("iTerm에서 해당 저장소로 이동해 \(agent.invocation)로 분석 내용을 전달합니다")
                }
                Button("기존 세션 연결") { sessionLink = row }
                    .buttonStyle(.bordered).disabled(launching != nil)
                Button("완료 처리") {
                    launching = row.id
                    Task {
                        launchMessages[row.id] = await store.completeHomeTask(row)
                        launching = nil
                    }
                }.buttonStyle(.bordered).disabled(launching != nil)
                if launching == row.id { ProgressView().controlSize(.small) }
            }
            if let message = launchMessages[row.id] {
                Text(message).f(.caption).foregroundStyle(.secondary)
            }
            Text(row.currentAI ? "Claude 분석 참고 · 검증 결과와 리뷰 원문으로 판단하세요" : row.insight?.status == "stale" ? "새 변경 감지 · Claude 재분석 대기" : "GitHub 상태에 따른 안내")
                .f(.caption2).foregroundStyle(.secondary)
        }.padding(.vertical, 3)
    }
}

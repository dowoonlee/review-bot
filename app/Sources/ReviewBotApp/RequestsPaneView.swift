import SwiftUI

struct RequestsSyncStatus: View {
    @Environment(BotStore.self) private var store

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(spacing: 6) {
                if store.isRefreshingStatus { ProgressView().controlSize(.mini) }
                if let date = store.statusLoadedAt {
                    Text("마지막 조회 \(date.formatted(date: .omitted, time: .standard))")
                } else {
                    Text(store.isRefreshingStatus ? "리뷰 요청을 조회하고 있습니다" : "아직 조회하지 않았습니다")
                }
                Spacer()
                Button("새로고침") { Task { await store.refreshStatus() } }
                    .disabled(store.isRefreshingStatus)
            }
            .f(.caption).foregroundStyle(.secondary)
            if let error = store.statusError {
                Text(error).f(.caption).foregroundStyle(.orange).textSelection(.enabled)
            }
            ForEach(store.status?.outgoingErrors ?? [], id: \.self) { error in
                Text(error).f(.caption).foregroundStyle(.orange).textSelection(.enabled)
            }
            if !(store.status?.outgoingFailedAccounts ?? []).isEmpty {
                Text("조회에 실패한 계정은 이전 결과가 있으면 유지합니다. 새로고침해 다시 확인하세요.")
                    .f(.caption).foregroundStyle(.secondary)
            }
        }
    }
}

struct SentReviewBadge: View {
    let pr: SentPR
    var body: some View {
        Text(pr.reviewLabel)
            .f(.caption2, .semibold)
            .padding(.horizontal, 7).padding(.vertical, 3)
            .foregroundStyle(color)
            .background(color.opacity(0.12), in: Capsule())
    }
    private var color: Color {
        switch pr.reviewDecision {
        case "CHANGES_REQUESTED": .orange
        case "APPROVED": .green
        default: .secondary
        }
    }
}

struct RequestsPaneView: View {
    @Environment(BotStore.self) private var store
    @Binding var selection: Selection?
    let initiallySent: Bool
    @State private var search = ""
    @State private var repo = ""

    private var received: [PendingPR] {
        (store.status?.pending ?? []).filter { matches($0.repo, $0.title, $0.number) }
    }
    private var sent: [SentPR] {
        store.sentPRs.filter { matches($0.repo, $0.title, $0.number) }
    }
    private var repos: [String] {
        Array(Set(initiallySent ? store.sentPRs.map(\.repo) : (store.status?.pending ?? []).map(\.repo))).sorted()
    }
    private func matches(_ slug: String, _ title: String, _ number: Int) -> Bool {
        (repo.isEmpty || repo == slug) && (search.isEmpty ||
            "\(slug) #\(number) \(title)".localizedCaseInsensitiveContains(search))
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("리뷰 요청").f(.title2, .semibold)
            Picker("요청 방향", selection: Binding(
                get: { initiallySent },
                set: { selection = .requests($0) }
            )) {
                Text("받은 요청 (\(store.status?.pending.count ?? 0))").tag(false)
                Text("보낸 요청 (\(store.sentPRs.count))").tag(true)
            }
            .pickerStyle(.segmented)
            Text(initiallySent ? "내가 작성한 열린 PR 중 리뷰를 요청한 이력이 있는 PR입니다. 수정 필요, 리뷰 대기 순으로 표시합니다."
                 : "나에게 리뷰를 요청한 열린 PR입니다. 봇의 처리 상태를 함께 표시합니다.")
                .f(.caption).foregroundStyle(.secondary)
            RequestsSyncStatus()
            HStack {
                TextField("제목, 저장소, PR 번호 검색", text: $search)
                    .textFieldStyle(.roundedBorder)
                Picker("저장소", selection: $repo) {
                    Text("전체 저장소").tag("")
                    ForEach(repos, id: \.self) { Text($0).tag($0) }
                }
                .frame(maxWidth: 280)
            }
            if (initiallySent && sent.isEmpty) || (!initiallySent && received.isEmpty) {
                ContentUnavailableView {
                    Label(store.isRefreshingStatus ? "조회 중입니다" : "표시할 요청이 없습니다", systemImage: "tray")
                } description: {
                    Text(search.isEmpty && repo.isEmpty ? "조회 상태를 확인하거나 새로고침하세요." : "검색어나 저장소 필터를 바꿔 보세요.")
                }
            } else {
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 0) {
                        if initiallySent {
                            ForEach(sent) { pr in
                                Button { selection = .sent(pr.id) } label: { sentRow(pr) }
                                    .buttonStyle(.plain)
                                Divider()
                            }
                        } else {
                            ForEach(received) { pr in
                                Button { selection = .pending(pr.id) } label: { receivedRow(pr) }
                                    .buttonStyle(.plain)
                                Divider()
                            }
                        }
                    }
                }
            }
        }
        .padding(20)
    }

    private func sentRow(_ pr: SentPR) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack(alignment: .top) {
                Text(pr.title).f(.body, .medium).lineLimit(2)
                Spacer()
                SentReviewBadge(pr: pr)
                Image(systemName: "chevron.right").foregroundStyle(.tertiary)
            }
            Text(pr.label).f(.caption).foregroundStyle(.secondary)
            Text(pr.reviewers.isEmpty ? "현재 지정된 리뷰어 없음" :
                 pr.reviewers.map { "\($0.name) \($0.label)" }.joined(separator: " · "))
                .f(.caption).lineLimit(2)
            HStack(spacing: 12) {
                if pr.isDraft { Text("초안 PR") }
                Text(pr.checksLabel)
                if pr.mergeable == "CONFLICTING" { Text("충돌 있음").foregroundStyle(.orange) }
                if let date = pr.requestedAt.flatMap({ ISO8601DateFormatter().date(from: $0) }) {
                    Text("최근 요청 \(date.formatted(.relative(presentation: .named)))")
                }
            }
            .f(.caption2).foregroundStyle(.secondary)
        }
        .padding(.vertical, 13).frame(maxWidth: .infinity, alignment: .leading)
        .contentShape(Rectangle())
    }

    private func receivedRow(_ pr: PendingPR) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack {
                Text(pr.title).f(.body, .medium).lineLimit(2)
                Spacer()
                Text(receivedStatus(pr)).f(.caption).foregroundStyle(.secondary)
                Image(systemName: "chevron.right").foregroundStyle(.tertiary)
            }
            Text("\(pr.repo)#\(pr.number) · \(pr.author) 작성").f(.caption).foregroundStyle(.secondary)
            if let by = pr.requestedBy { Text("\(by) 님이 리뷰 요청").f(.caption) }
        }
        .padding(.vertical, 13).frame(maxWidth: .infinity, alignment: .leading)
        .contentShape(Rectangle())
    }
    private func receivedStatus(_ pr: PendingPR) -> String {
        switch pr.status {
        case "posted": "봇 리뷰 게시됨"
        case "draft": "봇 초안 검토"
        case "unwatched": "자동 리뷰 대상 아님"
        default: "새 요청"
        }
    }
}

struct SentPRDetailView: View {
    let pr: SentPR
    var showList: (() -> Void)? = nil
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                if let showList {
                    Button(action: showList) { Label("보낸 요청 목록", systemImage: "chevron.left") }
                        .buttonStyle(.plain).f(.caption).foregroundStyle(.secondary)
                }
                Text(pr.label).f(.caption).foregroundStyle(.secondary)
                Text(pr.title).f(.title2, .semibold).textSelection(.enabled)
                HStack {
                    SentReviewBadge(pr: pr)
                    if pr.isDraft { Text("초안 PR").f(.caption) }
                    Text("\(pr.author) 작성").f(.caption).foregroundStyle(.secondary)
                }
                RequestsSyncStatus()
                Divider()
                Text("리뷰어별 현황").f(.headline)
                if pr.reviewers.isEmpty {
                    Text("현재 지정된 리뷰어나 표시할 리뷰가 없습니다.").foregroundStyle(.secondary)
                }
                ForEach(pr.reviewers) { reviewer in
                    HStack {
                        Label(reviewer.name, systemImage: "person")
                        Spacer()
                        Text(reviewer.label)
                            .foregroundStyle(reviewer.state == "CHANGES_REQUESTED" ? Color.orange : .secondary)
                    }
                    .f(.callout)
                }
                Divider()
                HStack(spacing: 16) {
                    Label(pr.checksLabel, systemImage: "checklist")
                    Text(pr.mergeable == "CONFLICTING" ? "충돌 있음" :
                         pr.mergeable == "MERGEABLE" ? "충돌 없음" : "충돌 여부 확인 중")
                }
                .f(.callout)
                Text("승인 상태와 CI·충돌 상태는 별도로 표시합니다. 최종 머지 조건과 리뷰 내용은 GitHub에서 확인하세요.")
                    .f(.caption).foregroundStyle(.secondary)
                if let at = pr.requestedAt.flatMap({ ISO8601DateFormatter().date(from: $0) }) {
                    Text("최근 리뷰 요청: \(at.formatted(date: .abbreviated, time: .shortened))")
                        .f(.caption).foregroundStyle(.secondary)
                }
                if let url = URL(string: pr.url) {
                    Link(destination: url) {
                        Label("GitHub에서 열기", systemImage: "arrow.up.forward.square")
                    }
                    .buttonStyle(.bordered)
                }
            }
            .padding(20).frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

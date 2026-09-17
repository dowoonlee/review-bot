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

private struct ReviewerStatusRow: View {
    let reviewer: PRReviewer

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: "person.fill").f(.body).foregroundStyle(.secondary)
            Text(reviewer.name).f(.title3, .semibold)
                .fixedSize(horizontal: false, vertical: true)
            Text(reviewer.label).f(.body, .medium)
                .foregroundStyle(statusColor)
                .fixedSize()
        }
    }

    private var statusColor: Color {
        switch reviewer.state {
        case "APPROVED": .green
        case "CHANGES_REQUESTED": .orange
        case "PENDING": .secondary
        default: .blue
        }
    }
}

private struct ReviewerGroup: Identifiable {
    let name: String                        // 빈 문자열이면 리뷰어가 없는 PR 묶음
    let entries: [(SentPR, PRReviewer?)]
    var id: String { name }
    /// "변경 요청 1 · 승인 2 · 대기 3" 처럼 이 리뷰어의 상태별 건수를 요약한다.
    var summary: String {
        var counts: [String: Int] = [:]
        for (_, reviewer) in entries { if let r = reviewer { counts[r.label, default: 0] += 1 } }
        return ["변경 요청", "코멘트", "승인", "대기"].compactMap { label in
            counts[label].map { "\(label) \($0)" }
        }.joined(separator: " · ")
    }
}

private enum ReviewFilter: String, CaseIterable {
    case all = "전체 리뷰 상태"
    case changes = "수정 필요"
    case approved = "승인 충족"
    case pending = "리뷰 대기"
    case other = "리뷰 확인"

    func matches(_ pr: SentPR) -> Bool {
        self == .all || pr.reviewLabel == rawValue
    }
}

private enum PRConditionFilter: String, CaseIterable {
    case all = "전체 PR 조건"
    case conflict = "충돌 있음"
    case noConflict = "충돌 없음"
    case checksFailed = "CI 실패"
    case checksPending = "CI 대기"
    case checksPassed = "CI 통과"
    case checksUnknown = "CI 정보 없음"
    case draft = "초안 PR"

    func matches(_ pr: SentPR) -> Bool {
        switch self {
        case .all: true
        case .conflict: pr.mergeable == "CONFLICTING"
        case .noConflict: pr.mergeable == "MERGEABLE"
        case .draft: pr.isDraft
        default: pr.checksLabel == rawValue
        }
    }
}

private enum ReceivedFilter: String, CaseIterable {
    case all = "전체 처리 상태"
    case new = "새 요청"
    case draft = "봇 초안 검토"
    case posted = "봇 리뷰 게시됨"
    case unwatched = "자동 리뷰 대상 아님"

    func matches(_ pr: PendingPR) -> Bool {
        switch self {
        case .all: true
        case .new: pr.status == "new"
        case .draft: pr.status == "draft"
        case .posted: pr.status == "posted"
        case .unwatched: pr.status == "unwatched"
        }
    }
}

struct RequestsPaneView: View {
    @Environment(BotStore.self) private var store
    @Binding var selection: Selection?
    let initiallySent: Bool
    @State private var search = ""
    @State private var repo = ""
    @AppStorage("requests.reviewFilter") private var reviewFilter: ReviewFilter = .all
    @AppStorage("requests.conditionFilter") private var conditionFilter: PRConditionFilter = .all
    @AppStorage("requests.receivedFilter") private var receivedFilter: ReceivedFilter = .all
    @AppStorage("requests.groupByReviewer") private var groupByReviewer = false

    private var hasFilters: Bool {
        !search.isEmpty || !repo.isEmpty || (initiallySent
            ? reviewFilter != .all || conditionFilter != .all : receivedFilter != .all)
    }

    private var received: [PendingPR] {
        (store.status?.pending ?? []).filter { matches($0.repo, $0.title, $0.number) && receivedFilter.matches($0) }
    }
    private var sent: [SentPR] {
        store.sentPRs.filter {
            matches($0.repo, $0.title, $0.number) && reviewFilter.matches($0) && conditionFilter.matches($0)
        }
    }
    /// 리뷰어별 묶음. 한 PR 에 리뷰어가 여럿이면 각 리뷰어 아래 모두 나온다. 리뷰어가 없는 PR 은 맨 뒤 "리뷰어 없음".
    private var sentByReviewer: [ReviewerGroup] {
        var groups: [String: [(SentPR, PRReviewer?)]] = [:]
        for pr in sent {
            if pr.reviewers.isEmpty { groups["", default: []].append((pr, nil)); continue }
            for reviewer in pr.reviewers { groups[reviewer.name, default: []].append((pr, reviewer)) }
        }
        return groups.keys.sorted { a, b in
            if a.isEmpty != b.isEmpty { return b.isEmpty }
            return a.localizedCaseInsensitiveCompare(b) == .orderedAscending
        }.map { name in
            // 묶음 안에서는 그 리뷰어가 답을 남긴 PR 을 위로, 아직 대기 중인 PR 을 아래로 둔다.
            let entries = groups[name]!.sorted { a, b in
                let (pa, pb) = (a.1?.priority ?? 3, b.1?.priority ?? 3)
                if pa != pb { return pa < pb }
                return (a.0.priority, a.0.requestedAt ?? a.0.updatedAt, a.0.id)
                    < (b.0.priority, b.0.requestedAt ?? b.0.updatedAt, b.0.id)
            }
            return ReviewerGroup(name: name, entries: entries)
        }
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
            Text(initiallySent ? "내가 작성한 열린 PR 중 리뷰를 요청한 이력이 있는 PR입니다. 리뷰가 달린 PR과 승인된 PR을 먼저, 리뷰 대기는 뒤에 표시합니다."
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
            statusFilters
            HStack {
                Text("\(initiallySent ? sent.count : received.count)건 표시")
                    .f(.caption).foregroundStyle(.secondary)
                Spacer()
                if initiallySent {
                    Toggle("리뷰어별 묶기", isOn: $groupByReviewer)
                        .toggleStyle(.checkbox).f(.caption)
                }
                if hasFilters {
                    Button("필터 초기화") {
                        search = ""
                        repo = ""
                        if initiallySent { reviewFilter = .all; conditionFilter = .all }
                        else { receivedFilter = .all }
                    }
                    .f(.caption)
                }
            }
            if (initiallySent && sent.isEmpty) || (!initiallySent && received.isEmpty) {
                ContentUnavailableView {
                    Label(store.isRefreshingStatus ? "조회 중입니다" : "표시할 요청이 없습니다", systemImage: "tray")
                } description: {
                    Text(hasFilters ? "검색어, 저장소 또는 상태 필터를 바꿔 보세요." : "조회 상태를 확인하거나 새로고침하세요.")
                }
            } else {
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 0) {
                        if initiallySent && groupByReviewer {
                            // 묶음마다 카드로 감싸 리뷰어 경계가 한눈에 보이게 한다.
                            ForEach(sentByReviewer) { group in
                                VStack(alignment: .leading, spacing: 0) {
                                    reviewerHeader(group)
                                    ForEach(Array(group.entries.enumerated()), id: \.element.0.id) { index, entry in
                                        let (pr, reviewer) = entry
                                        Button { selection = .sent(pr.id) } label: { sentRow(pr, highlight: reviewer?.name) }
                                            .buttonStyle(.plain)
                                        if index < group.entries.count - 1 { Divider() }
                                    }
                                }
                                .padding(.horizontal, 14).padding(.bottom, 4)
                                .background(.quaternary.opacity(0.3), in: RoundedRectangle(cornerRadius: 10))
                                .padding(.bottom, 14)
                            }
                        } else if initiallySent {
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
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .onChange(of: repos) {
            if !repo.isEmpty && !repos.contains(repo) { repo = "" }
        }
    }

    @ViewBuilder
    private var statusFilters: some View {
        if initiallySent {
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 16) {
                    Picker("리뷰 상태", selection: $reviewFilter) {
                        ForEach(ReviewFilter.allCases, id: \.self) { Text($0.rawValue).tag($0) }
                    }
                    Picker("PR 조건", selection: $conditionFilter) {
                        ForEach(PRConditionFilter.allCases, id: \.self) { Text($0.rawValue).tag($0) }
                    }
                }
                Text("두 조건을 모두 만족하는 PR을 표시합니다. 예: 승인 충족 + 충돌 있음")
                    .f(.caption).foregroundStyle(.secondary)
            }
        } else {
            Picker("처리 상태", selection: $receivedFilter) {
                ForEach(ReceivedFilter.allCases, id: \.self) { Text($0.rawValue).tag($0) }
            }
            .frame(maxWidth: 360, alignment: .leading)
        }
    }

    private func reviewerHeader(_ group: ReviewerGroup) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: group.name.isEmpty ? "person.slash" : "person.fill")
                .f(.body).foregroundStyle(.secondary)
            Text(group.name.isEmpty ? "리뷰어 없음" : group.name).f(.title3, .semibold)
            Text("\(group.entries.count)건").f(.callout).foregroundStyle(.secondary)
            if !group.summary.isEmpty {
                Text("·").foregroundStyle(.tertiary)
                Text(group.summary).f(.callout).foregroundStyle(.secondary)
            }
            Spacer()
        }
        .padding(.top, 14).padding(.bottom, 10)
        .overlay(alignment: .bottom) { Divider() }
    }

    private func sentRow(_ pr: SentPR, highlight: String? = nil) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack(alignment: .top) {
                PRNumberBadge(number: pr.number)
                Text(pr.title).f(.body, .medium).lineLimit(2)
                Spacer()
                SentReviewBadge(pr: pr)
                Image(systemName: "chevron.right").foregroundStyle(.tertiary)
            }
            Text(pr.repo).f(.caption).foregroundStyle(.secondary)
            if pr.reviewers.isEmpty {
                Text("현재 지정된 리뷰어 없음").f(.body).foregroundStyle(.secondary)
            } else {
                VStack(alignment: .leading, spacing: 6) {
                    ForEach(pr.reviewers) { reviewer in
                        ReviewerStatusRow(reviewer: reviewer)
                            .opacity(highlight == nil || highlight == reviewer.name ? 1 : 0.45)
                    }
                }
                .padding(.vertical, 3)
            }
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
                PRNumberBadge(number: pr.number)
                Text(pr.title).f(.body, .medium).lineLimit(2)
                Spacer()
                Text(receivedStatus(pr)).f(.caption).foregroundStyle(.secondary)
                Image(systemName: "chevron.right").foregroundStyle(.tertiary)
            }
            Text("\(pr.repo) · \(pr.author) 작성").f(.caption).foregroundStyle(.secondary)
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
                HStack(spacing: 10) {
                    PRNumberBadge(number: pr.number)
                    Text(pr.repo).f(.callout).foregroundStyle(.secondary)
                }
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
                    ReviewerStatusRow(reviewer: reviewer)
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

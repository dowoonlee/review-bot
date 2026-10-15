import SwiftUI

/// 긴급도 배지. 점수는 봇이 정한다 — Jev 분류가 있으면 그 점수, 없으면 Priority 필드.
struct UrgencyBadge: View {
    let issue: AssignedIssue
    var body: some View {
        Text(issue.urgencyLabel)
            .f(.caption2, .semibold)
            .padding(.horizontal, 7).padding(.vertical, 3)
            .foregroundStyle(color)
            .background(color.opacity(0.14), in: Capsule())
            .fixedSize()
            .help(sourceHint)
            .accessibilityLabel("긴급도 \(issue.urgencyLabel) · \(sourceHint)")
    }
    private var color: Color {
        if issue.score >= 1.75 { return .red }
        if issue.score >= 1.25 { return .orange }
        if issue.score >= 0.75 { return .blue }
        return .secondary
    }
    private var sourceHint: String {
        switch issue.rank?.source {
        case "jev": "Jev 가 이슈 내용으로 매긴 긴급도 (\(String(format: "%.1f", issue.score)) / 2)"
        case "priority": "GitHub Priority 필드 (\(issue.priorityLabel)) 기준"
        default: "Priority 필드가 없어 보통으로 둡니다"
        }
    }
}

struct IssueNumberBadge: View {
    let number: Int
    var body: some View {
        Text("#\(String(number))")
            .f(.body, .bold, mono: true)
            .foregroundStyle(Color.accentColor)
            .padding(.horizontal, 8).padding(.vertical, 4)
            .background(Color.accentColor.opacity(0.12), in: RoundedRectangle(cornerRadius: 5))
            .fixedSize()
            .accessibilityLabel("이슈 번호 \(number)")
    }
}

private enum UrgencyFilter: String, CaseIterable {
    case all = "전체 긴급도"
    case urgent = "긴급"
    case high = "높음"
    case normal = "보통"
    case low = "낮음"
    func matches(_ issue: AssignedIssue) -> Bool { self == .all || issue.urgencyLabel == rawValue }
}

struct IssuesPaneView: View {
    @Environment(BotStore.self) private var store
    @State private var search = ""
    @State private var repo = ""
    @AppStorage("issues.urgencyFilter") private var urgencyFilter: UrgencyFilter = .all

    private var hasFilters: Bool { !search.isEmpty || !repo.isEmpty || urgencyFilter != .all }
    private var all: [AssignedIssue] { store.assignedIssues }
    private var shown: [AssignedIssue] {
        all.filter {
            (repo.isEmpty || repo == $0.repo) && urgencyFilter.matches($0) && (search.isEmpty ||
                "\($0.repo) #\($0.number) \($0.title) \($0.labels.joined(separator: " "))".localizedCaseInsensitiveContains(search))
        }
    }
    private var repos: [String] { Array(Set(all.map(\.repo))).sorted() }
    private var jevEnabled: Bool { store.status?.selectionSettings?.enabled ?? false }
    private var jevCounts: (ready: Int, total: Int) {
        let classified = all.filter { $0.jev != nil }
        return (classified.filter { $0.jev?.usable == true }.count, classified.count)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("담당 이슈").f(.title2, .semibold)
            Text("나에게 할당된 열린 이슈입니다. 긴급한 것부터 보여 줍니다 — Jev 가 이슈 내용으로 매긴 긴급도를 먼저 쓰고, 분류가 없으면 GitHub Priority 필드로 줄을 세웁니다.")
                .f(.caption).foregroundStyle(.secondary)
            RequestsSyncStatus()
            ForEach(store.status?.issueErrors ?? [], id: \.self) { error in
                Text(error).f(.caption).foregroundStyle(.orange).textSelection(.enabled)
            }
            jevLine
            HStack {
                TextField("제목, 저장소, 번호, 라벨 검색", text: $search)
                    .textFieldStyle(.roundedBorder)
                Picker("저장소", selection: $repo) {
                    Text("전체 저장소").tag("")
                    ForEach(repos, id: \.self) { Text($0).tag($0) }
                }
                .frame(maxWidth: 280)
                Picker("긴급도", selection: $urgencyFilter) {
                    ForEach(UrgencyFilter.allCases, id: \.self) { Text($0.rawValue).tag($0) }
                }
                .frame(maxWidth: 160)
            }
            HStack {
                Text("\(shown.count)건 표시 · 전체 \(all.count)건").f(.caption).foregroundStyle(.secondary)
                Spacer()
                if hasFilters {
                    Button("필터 초기화") { search = ""; repo = ""; urgencyFilter = .all }.f(.caption)
                }
            }
            if shown.isEmpty {
                ContentUnavailableView {
                    Label(store.isRefreshingStatus ? "조회 중입니다" : "표시할 이슈가 없습니다", systemImage: "checklist")
                } description: {
                    Text(hasFilters ? "검색어, 저장소 또는 긴급도 필터를 바꿔 보세요." : "조회 상태를 확인하거나 새로고침하세요.")
                }
            } else {
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 0) {
                        ForEach(shown) { issue in
                            Button { open(issue) } label: { row(issue) }
                                .buttonStyle(.plain)
                                .contextMenu {
                                    Button("GitHub에서 열기") { open(issue) }
                                    Button("링크 복사") {
                                        NSPasteboard.general.clearContents()
                                        NSPasteboard.general.setString(issue.url, forType: .string)
                                    }
                                }
                            Divider()
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
    private var jevLine: some View {
        if jevEnabled {
            let counts = jevCounts
            Text(counts.total == 0
                 ? "Jev 분류는 감시 저장소의 이슈만 대상입니다. 다음 틱부터 차례로 분류합니다."
                 : "Jev 분류 \(counts.ready)/\(counts.total) 완료 · 나머지는 Priority 필드로 정렬합니다. 틱마다 최대 10건씩 갱신합니다.")
                .f(.caption).foregroundStyle(.secondary)
        } else {
            Text("설정에서 Jev 를 등록하면 이슈 내용으로 긴급도를 매깁니다. 지금은 GitHub Priority 필드로 정렬합니다.")
                .f(.caption).foregroundStyle(.secondary)
        }
    }

    private func open(_ issue: AssignedIssue) {
        guard let url = URL(string: issue.url), url.scheme == "https", url.host == "github.com" else { return }
        NSWorkspace.shared.open(url)
    }

    private func row(_ issue: AssignedIssue) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack(alignment: .top, spacing: 8) {
                IssueNumberBadge(number: issue.number)
                UrgencyBadge(issue: issue)
                Text(issue.title).f(.body, .medium).lineLimit(2)
                Spacer()
                Image(systemName: "arrow.up.forward.square").foregroundStyle(.tertiary)
            }
            HStack(spacing: 6) {
                Text(issue.repo)
                Text("· \(issue.author) 작성")
                if let type = issue.type { Text("· \(type)") }
                if issue.watched != true { Text("· 감시 저장소 아님").foregroundStyle(.tertiary) }
            }
            .f(.caption).foregroundStyle(.secondary)
            if let jev = issue.jev {
                HStack(spacing: 6) {
                    Text(jev.stateLabel.map { "Jev: \($0)" } ?? jev.statusLabel)
                        .f(.caption).foregroundStyle(jev.usable ? Color.accentColor : .secondary)
                    if jev.incomplete == true { Text("· 일부만 분석").f(.caption2).foregroundStyle(.orange) }
                    if let error = jev.error, jev.status == "error" {
                        Text(error).f(.caption2).foregroundStyle(.orange)
                    }
                }
            }
            HStack(spacing: 12) {
                Text("Priority \(issue.priorityLabel)")
                    .foregroundStyle(issue.priority == nil ? AnyShapeStyle(.tertiary) : AnyShapeStyle(.secondary))
                if let due = issue.targetDate { Text("목표 \(due)") }
                else if let milestone = issue.milestone { Text(milestone) }
                if let comments = issue.comments, comments > 0 { Text("댓글 \(comments)") }
                if let date = ISO8601DateFormatter().date(from: issue.updatedAt) {
                    Text("갱신 \(date.formatted(.relative(presentation: .named)))")
                }
            }
            .f(.caption2).foregroundStyle(.secondary)
            if !issue.labels.isEmpty {
                HStack(spacing: 5) {
                    ForEach(issue.labels, id: \.self) { label in
                        Text(label).f(.caption2)
                            .padding(.horizontal, 6).padding(.vertical, 2)
                            .background(.quaternary.opacity(0.5), in: Capsule())
                    }
                }
            }
        }
        .padding(.vertical, 13).frame(maxWidth: .infinity, alignment: .leading)
        .contentShape(Rectangle())
    }
}

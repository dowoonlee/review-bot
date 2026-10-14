import SwiftUI

enum Selection: Hashable {
    case home
    case settings
    case requests(Bool)
    case sent(String)
    case draft(String)
    case run(String)
    case live(String)
    case pending(String)
    case log

    /// 창을 닫았다 열어도 보던 곳으로 돌아가기 위한 표현. id 는 사라질 수 있으므로
    /// 고정된 화면만 복원한다.
    var stored: String {
        switch self {
        case .home: "home"
        case .settings: "settings"
        case .requests(let sent): sent ? "sentRequests" : "receivedRequests"
        case .sent(let id): "sent:\(id)"
        case .log: "log"
        case .draft(let id): "draft:\(id)"
        case .run(let id): "run:\(id)"
        case .live(let id): "live:\(id)"
        case .pending(let id): "pending:\(id)"
        }
    }

    static func restore(_ raw: String?) -> Selection? {
        switch raw {
        case "home": .home
        case "settings": .settings
        case "sentRequests": .requests(true)
        case "receivedRequests": .requests(false)
        case "log": .log
        default: nil
        }
    }
}

struct DashboardView: View {
    @Environment(BotStore.self) private var store
    @Environment(Prefs.self) private var prefs
    @State private var selection: Selection? = .home
    @State private var didLandOnDraft = false
    @State private var expanded: Set<String> = []
    @State private var didSeedExpansion = false

    var body: some View {
        // 상태 바는 스플릿 뷰 바깥에 둔다. safeAreaInset 으로 얹으면 사이드바 목록의
        // 첫 항목과 섹션 머리글이 바 아래로 말려 들어가 잘린다.
        VStack(spacing: 0) {
            statusBar
            HSplitView {
                sidebar
                    .frame(minWidth: 240, idealWidth: 260, maxWidth: 280)
                detail
                    .frame(minWidth: 500, maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .onChange(of: store.runGroups.count) {
            // 처음 뜰 때만 가장 최근 PR 을 펼쳐 둔다. 그 뒤 접고 펴는 것은 사용자 몫이다.
            guard !didSeedExpansion, let newest = newestGroupID else { return }
            didSeedExpansion = true
            expanded.insert(newest)
        }
        .onChange(of: selection) {
            UserDefaults.standard.set(selection?.stored, forKey: "lastPane")
        }
        .task {
            if let saved = Selection.restore(UserDefaults.standard.string(forKey: "lastPane")) {
                selection = saved
            }
        }
        // 검토를 기다리는 초안이 있으면 그리로 데려간다. 켜 두고 잊는 것이
        // 이 모드의 유일한 실패 방식이라, 한 번은 눈앞에 놓는다.
        .onChange(of: store.drafts.count) {
            guard !didLandOnDraft, let first = store.drafts.first else { return }
            didLandOnDraft = true
            selection = .draft(first.id)
        }
    }

    // MARK: 상단 상태 바

    private var statusBar: some View {
        HStack(spacing: 14) {
            HStack(spacing: 7) {
                HealthDot(health: store.health, size: 9)
                Text(store.health.label).f(.body, .semibold)
            }

            if let c = store.current, store.health == .running {
                // 도는 중일 때 가장 알고 싶은 것은 남은 시간이 아니라 "무엇을 보고 있나" 다.
                // lock 이 살아 있을 때만 믿는다 — 죽은 프로세스가 남긴 파일일 수 있다.
                HStack(spacing: 6) {
                    Text(c.label).f(.caption, .medium)
                    Text("\(c.author ?? "?") 작성").f(.caption).foregroundStyle(.secondary)
                    if let by = c.requestedBy {
                        Text("· \(by) 요청").f(.caption).foregroundStyle(.secondary)
                    }
                }
            } else if let p = store.paused {
                Text(p.reason.isEmpty ? "일시정지" : p.reason)
                    .f(.caption).foregroundStyle(.orange)
            } else if let next = store.nextTick, store.health != .running {
                TimelineView(.periodic(from: .now, by: 1)) { _ in
                    let left = max(0, Int(next.timeIntervalSinceNow))
                    Text("다음 틱 \(left / 60):\(String(format: "%02d", left % 60))")
                        .f(.caption, digits: true).foregroundStyle(.secondary)
                }
                .help("launchd 주기(\(Int(store.tickInterval / 60))분) 기준 예상입니다. 손으로 돌린 틱은 launchd 시계를 건드리지 않아 이 값이 바뀌지 않고, 맥이 잠들면 깨어난 뒤로 밀립니다.")
            } else if let r = store.running, r.alive {
                Text("pid " + String(r.pid) + " · " + r.since).f(.caption).foregroundStyle(.secondary)
            }

            Divider().frame(height: 14)

            Text("오늘 \(store.todaysRuns.count)건")
                .f(.caption, digits: true).foregroundStyle(.secondary)

            Toggle(isOn: Binding(
                get: { store.autoPost },
                set: { on in Task { await store.setAutoPost(on) } }
            )) {
                Text(store.autoPost ? "자동 게시" : "검토 후 게시")
                    .f(.caption)
                    .foregroundStyle(store.autoPost ? Color.accentColor : .orange)
            }
            .toggleStyle(.switch)
            .controlSize(.mini)
            .disabled(store.busy != nil)
            .help(store.autoPost
                  ? "리뷰가 끝나면 바로 GitHub 에 올립니다"
                  : "초안만 남깁니다 — 읽어 보고 직접 게시합니다")

            Spacer()

            if let busy = store.busy {
                ProgressView().controlSize(.small)
                Text(busy).f(.caption).foregroundStyle(.secondary)
            }

            Button {
                Task { await store.run([store.paused == nil ? "pause" : "resume"],
                                       label: store.paused == nil ? "일시정지" : "재개") }
            } label: {
                Label(store.paused == nil ? "일시정지" : "재개",
                      systemImage: store.paused == nil ? "pause.fill" : "play.fill")
            }
            Button {
                Task { await store.tickNow() }
            } label: {
                Label("지금 틱", systemImage: "arrow.clockwise")
            }
            .disabled(store.paused != nil || store.health == .running)
            Button {
                Task { await store.refreshStatus() }
            } label: {
                Label("새로고침", systemImage: "arrow.trianglehead.2.clockwise")
            }

            Divider().frame(height: 14)

            Button { prefs.smaller() } label: { Image(systemName: "textformat.size.smaller") }
                .disabled(prefs.fontStep == 0)
                .help("글자 작게 (⌘−)")
            Button { prefs.bigger() } label: { Image(systemName: "textformat.size.larger") }
                .disabled(prefs.fontStep == Prefs.steps.count - 1)
                .help("글자 크게 (⌘+)")
            Button { prefs.pinned.toggle() } label: {
                Image(systemName: prefs.pinned ? "pin.fill" : "pin.slash")
            }
            .background(prefs.pinned ? Color.accentColor.opacity(0.25) : .clear,
                        in: RoundedRectangle(cornerRadius: 5))
            .help(prefs.pinned ? "항상 맨 위 — 켜짐 (⇧⌘T)" : "항상 맨 위에 두기 (⇧⌘T)")
            Button { selection = .settings } label: { Image(systemName: "gearshape") }
                .help("설정")
        }
        .buttonStyle(.bordered)
        .controlSize(.small)
        .padding(.horizontal, 14).padding(.vertical, 9)
        .background(.bar)
        .overlay(alignment: .bottom) { Divider() }
        .zIndex(1)
    }

    // MARK: 사이드바

    private var sidebar: some View {
        VStack(spacing: 0) {
            VStack(spacing: 4) {
                navigationRow(.home) { Label("홈", systemImage: "house") }
                navigationRow(.requests(false), active: isRequestSelection) {
                    Label("리뷰 요청", systemImage: "arrow.left.arrow.right")
                }
            }
            .padding(.horizontal, 10).padding(.top, 14).padding(.bottom, 12)
            Divider().padding(.horizontal, 14)

            ScrollView {
                LazyVStack(alignment: .leading, spacing: 16) {
                    if !store.live.isEmpty {
                        sidebarSection("라이브 세션") {
                            ForEach(store.live) { session in
                                navigationRow(.live(session.id)) {
                                    HStack(spacing: 8) {
                                        ProgressView().controlSize(.mini)
                                        VStack(alignment: .leading, spacing: 3) {
                                            Text(session.agent).f(.callout)
                                            Text("턴 \(session.turns) · \(session.toolSummary)")
                                                .f(.caption2).foregroundStyle(.secondary).lineLimit(1)
                                        }
                                    }
                                }
                            }
                        }
                    }
                    if !store.drafts.isEmpty {
                        sidebarSection("게시 대기 \(store.drafts.count)건") {
                            ForEach(store.drafts) { draft in
                                navigationRow(.draft(draft.id)) {
                                    VStack(alignment: .leading, spacing: 4) {
                                        HStack {
                                            Text(draft.label).f(.callout, .medium)
                                            Spacer()
                                            EventBadge(event: draft.event)
                                        }
                                        Text("인라인 \(draft.inline ?? 0) · 본문 \(draft.demoted ?? 0)")
                                            .f(.caption2).foregroundStyle(.secondary)
                                    }
                                }
                                .contextMenu {
                                    Button("이 PR의 초안·기록·검증 자료 정리", role: .destructive) {
                                        Task { await store.cleanPR(draft.repo, draft.number) }
                                    }
                                }
                            }
                        }
                    }
                    ForEach(groupedByRepo, id: \.repo) { bucket in
                        sidebarSection(bucket.repo) {
                            ForEach(bucket.groups) { group in
                                groupHeader(group)
                                    .padding(.horizontal, 10).padding(.vertical, 6)
                                if expanded.contains(group.id) {
                                    ForEach(group.runs) { run in
                                        navigationRow(.run(run.id)) { runRow(run) }
                                            .padding(.leading, 12)
                                            .contextMenu {
                                                Button("이 기록 삭제", role: .destructive) {
                                                    Task { await store.forget(run) }
                                                }
                                            }
                                    }
                                }
                            }
                        }
                    }
                }
                .padding(.horizontal, 10).padding(.vertical, 14)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .frame(maxHeight: .infinity)

            Divider().padding(.horizontal, 14)
            VStack(spacing: 4) {
                navigationRow(.log) { Label("틱 로그", systemImage: "text.alignleft") }
                navigationRow(.settings) { Label("설정 · 리뷰어와 도구", systemImage: "gearshape") }
            }
            .padding(.horizontal, 10).padding(.vertical, 12)
        }
        .frame(maxHeight: .infinity)
        .background(Color(nsColor: .controlBackgroundColor))
    }

    private var isRequestSelection: Bool {
        switch selection {
        case .requests, .sent, .pending: true
        default: false
        }
    }

    private func navigationRow<Content: View>(_ target: Selection, active: Bool? = nil,
                                              @ViewBuilder content: () -> Content) -> some View {
        let selected = active ?? (selection == target)
        return Button { selection = target } label: {
            content()
                .f(.callout, selected ? .semibold : .regular)
                .foregroundStyle(selected ? Color.accentColor : Color.primary)
                .frame(maxWidth: .infinity, minHeight: 22, alignment: .leading)
                .padding(.horizontal, 12).padding(.vertical, 8)
                .background(selected ? Color.accentColor.opacity(0.16) : .clear,
                            in: RoundedRectangle(cornerRadius: 7))
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(selected ? .isSelected : [])
    }

    private func sidebarSection<Content: View>(_ title: String,
                                               @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title).f(.caption2, .semibold).foregroundStyle(.secondary)
                .lineLimit(1).padding(.horizontal, 12).padding(.bottom, 4)
            content()
        }
    }

    /// PR 머리 행. 작은 삼각형만 누르게 하면 표적이 너무 좁아서, 행 전체를 눌러
    /// 접었다 펼 수 있게 한다 — 삼각형은 상태를 보여 주기만 한다.
    private func groupHeader(_ group: RunGroup) -> some View {
        Button {
            withAnimation(.easeInOut(duration: 0.15)) {
                if expanded.contains(group.id) { expanded.remove(group.id) }
                else { expanded.insert(group.id) }
            }
        } label: {
            HStack(spacing: 6) {
                Image(systemName: "chevron.right")
                    .rotationEffect(.degrees(expanded.contains(group.id) ? 90 : 0))
                    .f(.caption2).foregroundStyle(.tertiary)
                Text("#" + String(group.number)).f(.callout, .medium)
                if group.posted {
                    Image(systemName: "checkmark.circle.fill")
                        .f(.caption2).foregroundStyle(.green)
                }
                Spacer()
                Text(group.runs.count > 1 ? "\(group.runs.count)회" : "1회")
                    .f(.caption2).foregroundStyle(.tertiary)
            }
            .padding(.vertical, 2)
            .contentShape(Rectangle())      // 여백까지 클릭 대상으로
        }
        .buttonStyle(.plain)
        .contextMenu {
            Button("이 PR 이력 \(group.runs.count)건 삭제", role: .destructive) {
                Task { await store.forget(group: group) }
            }
            Button("이 PR의 초안·기록·검증 자료 정리", role: .destructive) {
                Task { await store.cleanPR(group.repo, group.number) }
            }
        }
    }

    /// 실행 한 건. 금액은 상단 바에 합계로 있으므로 여기서는 뺀다 — 목록에서 중요한 것은
    /// 언제·어느 커밋을·게시했는지다.
    private func runRow(_ r: RunRecord) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 5) {
                if r.failed {
                    Text("실패").f(.caption2).foregroundStyle(.red)
                } else {
                    EventBadge(event: r.event)
                    Text((r.posted ?? false) ? "게시" : r.event == "PENDING" ? "대기" : "초안")
                        .f(.caption2)
                        .foregroundStyle((r.posted ?? false) ? Color.green : .orange)
                }
                if r.partial {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .f(.caption2).foregroundStyle(.orange)
                }
                Spacer()
            }
            HStack(spacing: 4) {
                Text(shortTime(r.at))
                if let sha = r.sha, !r.failed {
                    Text("·")
                    Text(String(sha.prefix(7))).foregroundStyle(.tertiary)
                    Text("· 인라인 \(r.inline ?? 0)")
                }
            }
            .f(.caption2).foregroundStyle(.secondary)
        }
    }

    private struct RepoBucket { var repo: String; var groups: [RunGroup] }

    private var groupedByRepo: [RepoBucket] {
        var order: [String] = []
        var map: [String: [RunGroup]] = [:]
        for g in store.runGroups {
            if map[g.repo] == nil { order.append(g.repo) }
            map[g.repo, default: []].append(g)
        }
        return order.map { RepoBucket(repo: $0, groups: map[$0] ?? []) }
    }

    private var newestGroupID: String? {
        store.runGroups.max { ($0.latestAt) < ($1.latestAt) }?.id
    }

    // MARK: 상세

    @ViewBuilder
    private var detail: some View {
        switch selection {
        case .requests(let sent):
            RequestsPaneView(selection: $selection, initiallySent: sent)
                .id(sent)
        case .sent(let id):
            if let pr = store.sentPRs.first(where: { $0.id == id }) {
                SentPRDetailView(pr: pr) { selection = .requests(true) }
            }
            else { ContentUnavailableView("닫혔거나 목록에서 사라진 PR입니다", systemImage: "tray") }
        case .settings:
            SettingsPaneView()
        case .draft(let id):
            if let d = store.drafts.first(where: { $0.id == id }) { DraftDetailView(draft: d) }
            else { ContentUnavailableView("게시했거나 사라진 초안입니다", systemImage: "tray") }
        case .run(let id):
            if let r = store.runs.first(where: { $0.id == id }) { RunDetailView(run: r) }
            else { ContentUnavailableView("기록이 없습니다", systemImage: "tray") }
        case .live(let id):
            if let s = store.live.first(where: { $0.id == id }) { LiveDetailView(session: s) }
            else { ContentUnavailableView("세션이 끝났습니다", systemImage: "moon.zzz") }
        case .pending(let id):
            if let p = store.status?.pending.first(where: { $0.id == id }) { PendingDetailView(pr: p) }
            else { ContentUnavailableView("목록에서 사라졌습니다", systemImage: "tray") }
        case .log:
            LogPaneView()
        case .home, .none:
            HomePaneView(selection: $selection)
        }
    }
}

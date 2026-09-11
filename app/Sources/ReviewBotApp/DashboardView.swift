import SwiftUI

enum Selection: Hashable {
    case home
    case settings
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
            NavigationSplitView {
                sidebar
                    .navigationSplitViewColumnWidth(min: 240, ideal: 280, max: 360)
            } detail: {
                detail
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
        List(selection: $selection) {
            Label("홈", systemImage: "house").tag(Selection.home)

            if !store.live.isEmpty {
                Section("라이브 세션") {
                    ForEach(store.live) { s in
                        HStack(spacing: 6) {
                            ProgressView().controlSize(.mini)
                            VStack(alignment: .leading, spacing: 1) {
                                HStack(spacing: 5) {
                                    Text(s.agent).f(.callout)
                                    if let c = store.current {
                                        Text(c.label).f(.caption2).foregroundStyle(.secondary)
                                    }
                                }
                                Text("턴 " + String(s.turns) + " · " + s.toolSummary)
                                    .f(.caption2).foregroundStyle(.secondary).lineLimit(1)
                            }
                        }
                        .tag(Selection.live(s.id))
                    }
                }
            }

            if !store.drafts.isEmpty {
                Section("게시 대기 \(store.drafts.count)건") {
                    ForEach(store.drafts) { d in
                        VStack(alignment: .leading, spacing: 2) {
                            HStack(spacing: 5) {
                                Image(systemName: "tray.full")
                                    .f(.caption2).foregroundStyle(.orange)
                                Text(d.label).f(.callout, .medium)
                                EventBadge(event: d.event)
                                Spacer()
                            }
                            Text("인라인 \(d.inline ?? 0) · 본문 \(d.demoted ?? 0) · \(shortTime(d.at))")
                                .f(.caption2).foregroundStyle(.secondary)
                        }
                        .tag(Selection.draft(d.id))
                    }
                }
            }

            if let pending = store.status?.pending, !pending.isEmpty {
                Section("대기 중인 리뷰 요청") {
                    ForEach(pending) { p in
                        VStack(alignment: .leading, spacing: 1) {
                            HStack(spacing: 5) {
                                Image(systemName: icon(for: p.status))
                                    .f(.caption2).foregroundStyle(color(for: p.status))
                                Text(p.label).f(.callout)
                            }
                            Text(p.title).f(.caption2).foregroundStyle(.secondary).lineLimit(1)
                        }
                        .tag(Selection.pending(p.id))
                    }
                }
            }

            // 이력은 저장소 → PR 로 묶는다. 한 PR 이 여러 번 도는 것은 흔한 일이라
            // 평평한 목록으로 두면 왜 여러 개인지 읽히지 않는다.
            ForEach(groupedByRepo, id: \.repo) { bucket in
                Section(bucket.repo) {
                    ForEach(bucket.groups) { group in
                        groupHeader(group)
                        if expanded.contains(group.id) {
                            ForEach(group.runs) { r in
                                runRow(r)
                                    .tag(Selection.run(r.id))
                                    .contextMenu {
                                        Button("이 기록 삭제", role: .destructive) {
                                            Task { await store.forget(r) }
                                        }
                                    }
                            }
                        }
                    }
                }
            }

            Section {
                Label("틱 로그", systemImage: "text.alignleft").tag(Selection.log)
                Label("설정 · 리뷰어와 도구", systemImage: "gearshape").tag(Selection.settings)
            }
        }
        .listStyle(.sidebar)
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
                    Text((r.posted ?? false) ? "게시" : "초안")
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

    private func icon(for status: String) -> String {
        switch status {
        case "posted": "checkmark.circle.fill"
        case "draft": "doc.text"
        case "new": "circle"
        default: "eye.slash"
        }
    }
    private func color(for status: String) -> Color {
        switch status {
        case "posted": .green
        case "draft": .orange
        case "new": .blue
        default: .secondary
        }
    }

    // MARK: 상세

    @ViewBuilder
    private var detail: some View {
        switch selection {
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

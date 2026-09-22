import AppKit
import SwiftUI
import UniformTypeIdentifiers

/// 봇이 지금 어떤 설정으로 도는지 한 화면에 모은다.
/// 값의 출처는 config.json 과 ~/.claude/agents/*.md 이고, 앱은 그것을 비추기만 한다.
struct SettingsPaneView: View {
    @Environment(BotStore.self) private var store
    @Environment(Prefs.self) private var prefs
    @State private var minutes: Int = 15
    @State private var cap: Int = 2
    @State private var clearing = false
    @State private var dropTarget = false
    @State private var newRepo = ""
    @State private var newRepoPath = ""
    @State private var newAccount = ""
    @State private var confirmRemoveAgent: String?
    @State private var confirmRemoveRepo: String?

    var body: some View {
        // Stepper 같은 포커스 가능한 컨트롤이 있으면 AppKit 이 그것을 화면 안으로
        // 끌어와서 첫 화면이 중간부터 보인다. 열릴 때 맨 위로 되돌린다.
        ScrollViewReader { proxy in
            scroller
                .onAppear { proxy.scrollTo("top", anchor: .top) }
                .task { proxy.scrollTo("top", anchor: .top) }
        }
    }

    private var scroller: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                Color.clear.frame(height: 1).id("top")
                appearance
                Divider()
                posting
                Divider()
                cadence
                Divider()
                reviewers
                Divider()
                repositories
                Divider()
                tools
                Divider()
                history
                Divider()
                account
            }
            .padding(20)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .defaultScrollAnchor(.top)
        .task(id: store.config?.intervalSec) {
            if let s = store.config?.intervalSec, s > 0 { minutes = max(1, s / 60) }
        }
        .task(id: store.config?.maxPerTick) {
            if let n = store.config?.maxPerTick, n > 0 { cap = min(20, n) }
        }
    }

    // MARK: 보기

    private var appearance: some View {
        section("보기") {
            LabeledContent("글자 크기") {
                HStack(spacing: 8) {
                    Button { prefs.smaller() } label: { Image(systemName: "textformat.size.smaller") }
                        .disabled(prefs.fontStep == 0)
                    Text(prefs.stepLabel).f(.callout, digits: true)
                        .frame(width: 74)
                    Button { prefs.bigger() } label: { Image(systemName: "textformat.size.larger") }
                        .disabled(prefs.fontStep == Prefs.steps.count - 1)
                    Text("⌘+ / ⌘−").f(.caption2).foregroundStyle(.tertiary)
                }
                .buttonStyle(.bordered)
            }
            Toggle("항상 맨 위에 두기 (⇧⌘T)", isOn: Binding(
                get: { prefs.pinned }, set: { prefs.pinned = $0 }))
            Toggle("알림 받기 — 새 리뷰 요청, 초안 완료", isOn: Binding(
                get: { prefs.notify }, set: { prefs.notify = $0 }))

            let n = Notifier.shared
            HStack(spacing: 8) {
                Image(systemName: n.status.ok ? "bell.badge" : "bell.slash")
                    .foregroundStyle(n.status.ok ? Color.green : .orange)
                Text("알림 권한 \(n.status.label)\(n.attempts > 1 ? " · \(n.attempts)회 시도" : "")")
                    .f(.caption).foregroundStyle(.secondary)
                Spacer()
                Button("테스트 알림") {
                    Notifier.shared.post(title: "review-bot",
                                         body: "알림이 이렇게 옵니다 — 소리는 배너가 막혀도 납니다")
                }
                Button("시스템 설정") {
                    if let url = URL(string: "x-apple.systempreferences:com.apple.Notifications-Settings.extension") {
                        NSWorkspace.shared.open(url)
                    }
                }
            }
            .buttonStyle(.bordered)
            .controlSize(.small)
            if let last = n.lastSent {
                Text("마지막 알림 · \(last)").f(.caption2).foregroundStyle(.tertiary)
            }
            if !n.status.ok {
                Text("서명 없이 만든 앱은 알림 센터 등록이 거절될 수 있습니다. 그때도 소리는 나고, 메뉴바 아이콘에 대기 건수가 붙습니다.")
                    .f(.caption).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    // MARK: 게시

    private var posting: some View {
        section("게시") {
            Toggle(isOn: Binding(get: { store.autoPost },
                                 set: { on in Task { await store.setAutoPost(on) } })) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(store.autoPost ? "자동 게시" : "검토 후 게시")
                    Text(store.autoPost
                         ? "리뷰가 끝나면 판정대로 바로 GitHub 에 올립니다"
                         : "초안만 남깁니다 — 읽어 보고 직접 게시합니다")
                        .f(.caption).foregroundStyle(.secondary)
                }
            }
            .disabled(store.busy != nil)

            if let c = store.config {
                row("게시할 최소 등급", c.minSeverity ?? "-")
                row("PR 당 최대 지적", "\(c.maxFindingsPerPR ?? 0)건")
                row("판정이 낼 수 있는 이벤트", (c.allowedEvents ?? []).joined(separator: " · "))
                row("판정 자율", (c.autonomous ?? true) ? "예 — 봇이 이벤트를 스스로 정합니다" : "아니오")
                row("판정 모델", c.judgeModel ?? "-")
            }
        }
    }

    // MARK: 틱 주기

    /// 적용 버튼을 눌러 봐야 소용없는 상태 — 이미 그 값이거나, 명령이 돌고 있다.
    private var intervalIdle: Bool {
        store.busy != nil || minutes * 60 == (store.config?.intervalSec ?? 900)
    }

    private var capIdle: Bool {
        store.busy != nil || cap == (store.config?.maxPerTick ?? 2)
    }

    private func applyInterval() { if !intervalIdle { Task { await store.setInterval(minutes: minutes) } } }
    private func applyCap() { if !capIdle { Task { await store.setMaxPerTick(cap) } } }

    /// 키패드로 직접 치거나 ↑↓ 로 올리거나 — 둘 다 되는 숫자 칸. 범위 밖 값은 치는
    /// 즉시 잘라 준다. CLI 도 거절하지만, 거절당하러 가기 전에 막는 편이 친절하다.
    private func numberField(_ value: Binding<Int>, range: ClosedRange<Int>,
                             unit: String) -> some View {
        HStack(spacing: 4) {
            TextField("", value: value, format: .number)
                .textFieldStyle(.roundedBorder)
                .multilineTextAlignment(.trailing)
                .frame(width: 56)
                .f(.callout, digits: true)
                .onChange(of: value.wrappedValue) { _, new in
                    let fixed = min(range.upperBound, max(range.lowerBound, new))
                    if fixed != new { value.wrappedValue = fixed }
                }
            Text(unit).f(.callout).foregroundStyle(.secondary)
            Stepper("", value: value, in: range).labelsHidden()
        }
        .disabled(store.busy != nil)
    }

    private var cadence: some View {
        section("틱 주기와 상한") {
            LabeledContent("몇 분마다 훑을까") {
                HStack(spacing: 10) {
                    numberField($minutes, range: 1...240, unit: "분")
                    Button("적용", action: applyInterval)
                        .buttonStyle(.borderedProminent)
                        .disabled(intervalIdle)
                    if store.busy != nil { ProgressView().controlSize(.small) }
                }
                .onSubmit(applyInterval)
            }
            Text(verbatim: "1~240분. launchd 의 StartInterval 을 바꾸고 job 을 다시 올립니다. 틱이 도는 중이면 거절합니다.")
                .f(.caption).foregroundStyle(.secondary)

            LabeledContent("틱 한 번에 몇 건까지") {
                HStack(spacing: 10) {
                    numberField($cap, range: 1...20, unit: "건")
                    Button("적용", action: applyCap)
                        .buttonStyle(.borderedProminent)
                        .disabled(capIdle)
                    if store.busy != nil { ProgressView().controlSize(.small) }
                }
                .onSubmit(applyCap)
            }
            Text(verbatim: "1~20건. 도는 틱은 이전 상한으로 끝내고 다음 틱부터 적용됩니다.")
                .f(.caption).foregroundStyle(.secondary)

            if let c = store.config {
                row("지금 값", "\((c.intervalSec ?? 0) / 60)분마다 · 최대 \(c.maxPerTick ?? 0)건")
            }
        }
    }

    // MARK: 리뷰어

    private var reviewers: some View {
        section("리뷰어") {
            ForEach(store.config?.agents ?? []) { agent in
                VStack(alignment: .leading, spacing: 6) {
                    HStack(spacing: 7) {
                        Image(systemName: agent.exists ? "person.crop.circle" : "exclamationmark.triangle")
                            .foregroundStyle(agent.exists ? Color.accentColor : .red)
                        Text(agent.name).f(.callout, .semibold)
                        Spacer()
                        Text("모델 \(agent.runsAs ?? store.config?.model ?? "-")")
                            .f(.caption2).foregroundStyle(.secondary)
                    }
                    if let err = agent.error {
                        Text(err).f(.caption).foregroundStyle(.red)
                    }
                    if !agent.description.isEmpty {
                        Text(agent.description).f(.caption).foregroundStyle(.secondary)
                    }
                    if !agent.tools.isEmpty {
                        HStack(spacing: 5) {
                            Text("정의 파일의 도구(참고)").f(.caption2).foregroundStyle(.tertiary)
                            ForEach(agent.tools, id: \.self) { t in
                                Text(t).f(.caption2, mono: true)
                                    .padding(.horizontal, 5).padding(.vertical, 1)
                                    .background(.quaternary, in: Capsule())
                            }
                        }
                    }
                    if !agent.role.isEmpty {
                        Text(agent.role).f(.caption).foregroundStyle(.secondary)
                            .lineLimit(4)
                    }
                    if let path = agent.path {
                        Text(path).f(.caption2, mono: true).foregroundStyle(.tertiary)
                            .textSelection(.enabled)
                    }
                    HStack(spacing: 8) {
                        Picker("모델", selection: Binding(
                            get: { agent.runsAs ?? "sonnet" },
                            set: { m in Task { await store.setAgentModel(agent.name, m) } }
                        )) {
                            ForEach(["haiku", "sonnet", "opus"], id: \.self) { Text($0).tag($0) }
                        }
                        .labelsHidden().frame(width: 110)
                        Spacer()
                        Button(role: .destructive) {
                            confirmRemoveAgent = agent.name
                        } label: { Label("해제", systemImage: "minus.circle") }
                            .f(.caption)
                    }
                    .disabled(store.busy != nil)
                }
                .padding(12)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(.quaternary.opacity(0.3), in: RoundedRectangle(cornerRadius: 8))
            }

            // 리뷰어를 늘리는 유일한 경로. 정의 파일 하나가 곧 리뷰어 하나다.
            VStack(spacing: 6) {
                Image(systemName: "doc.badge.plus").f(.title3)
                    .foregroundStyle(dropTarget ? Color.accentColor : .secondary)
                Text("에이전트 정의(.md)를 여기에 끌어다 놓으면 등록됩니다")
                    .f(.caption).foregroundStyle(.secondary)
                Button("파일 고르기…") { chooseAgentFile() }
                    .f(.caption).disabled(store.busy != nil)
            }
            .padding(14)
            .frame(maxWidth: .infinity)
            .background(dropTarget ? Color.accentColor.opacity(0.12) : Color.clear,
                        in: RoundedRectangle(cornerRadius: 8))
            .overlay(RoundedRectangle(cornerRadius: 8)
                .strokeBorder(dropTarget ? Color.accentColor : Color.secondary.opacity(0.35),
                              style: StrokeStyle(lineWidth: 1, dash: [4])))
            .onDrop(of: [.fileURL], isTargeted: $dropTarget) { providers in
                guard let provider = providers.first else { return false }
                _ = provider.loadObject(ofClass: URL.self) { url, _ in
                    guard let url, url.pathExtension.lowercased() == "md" else { return }
                    Task { await store.addAgent(path: url.path) }
                }
                return true
            }
            .alert("리뷰어를 해제할까요?", isPresented: Binding(
                get: { confirmRemoveAgent != nil },
                set: { if !$0 { confirmRemoveAgent = nil } })) {
                Button("해제", role: .destructive) {
                    if let n = confirmRemoveAgent { Task { await store.removeAgent(n) } }
                    confirmRemoveAgent = nil
                }
                Button("취소", role: .cancel) { confirmRemoveAgent = nil }
            } message: {
                Text("\(confirmRemoveAgent ?? "") 를 리뷰에서 뺍니다. 정의 파일은 지우지 않습니다.")
            }
            if let c = store.config {
                row("동시 실행", (c.parallel ?? true) ? "병렬 (최대 \(c.workers ?? 2)개)" : "순차 (메모리 절약)")
                row("묶음 간 통합", c.integrationMode == "all" ? "리뷰어별 독립 검토" : "모든 관점을 모아 1회 검토")
                row("턴 상한", (c.maxTurns ?? 0) == 0 ? "없음" : "\(c.maxTurns ?? 0)턴")
                row("검토 호출당 시간 상한", "\((c.timeoutSec ?? 0) / 60)분")
                let fileLimit = (c.maxChangedFiles ?? 0) > 0 ? "파일 \(c.maxChangedFiles ?? 0)개" : "파일 제한 없음"
                let lineLimit = (c.maxDiffLines ?? 0) > 0 ? "\(c.maxDiffLines ?? 0)줄" : "줄 제한 없음"
                row("PR 크기 상한", "\(fileLimit) · \(lineLimit)")
                if let files = c.batchMaxFiles, let chars = c.batchMaxChars {
                    row("검토 묶음", "파일 \(files)개 · diff \(chars)자")
                }
            }
        }
    }

    // MARK: 도구

    private var tools: some View {
        section("봇이 리뷰어에게 주는 도구") {
            HStack(spacing: 5) {
                ForEach(store.config?.botTools ?? [], id: \.self) { t in
                    Text(t).f(.caption, mono: true)
                        .padding(.horizontal, 7).padding(.vertical, 2)
                        .background(Color.accentColor.opacity(0.15), in: Capsule())
                }
            }
            Text("리뷰는 남의 PR 브랜치를 체크아웃한 워크트리에서 돕니다. 그래서 `--tools` 로 도구 집합 자체를 이 셋으로 정하고, `--restricted` 로 코드 실행·WebFetch 를 없애고 파일 접근을 작업 디렉터리 안에 가두며, `--strict-mcp-config` 로 MCP 서버를 전부 뺍니다. 정의 파일에 무엇이 적혀 있든 리뷰어가 쥐는 도구는 이 셋뿐이고, Bash 는 존재하지 않습니다.")
                .f(.caption).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    // MARK: 이력

    private var history: some View {
        section("이력") {
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    Text("기록 \(store.runs.count)건").f(.callout)
                    Text("목록에서 PR 이나 기록을 오른쪽 클릭하면 하나씩도 지웁니다. 지워도 처리 지문은 남으므로 다시 리뷰하지 않습니다.")
                        .f(.caption).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer()
                Button("전체 비우기", role: .destructive) { clearing = true }
                    .buttonStyle(.bordered)
                    .disabled(store.busy != nil || store.runs.isEmpty)
            }
            Text("틱마다 자동으로도 정리합니다 — claude 세션 3일, 초안 14일, 이력 2000줄, 로그 5MB, 처리 지문 90일이 지나면 지웁니다.")
                .f(.caption).foregroundStyle(.tertiary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .confirmationDialog("이력을 전부 지울까요?", isPresented: $clearing) {
            Button("전체 삭제", role: .destructive) { Task { await store.forgetAll() } }
            Button("취소", role: .cancel) {}
        } message: {
            Text("실행 기록 \(store.runs.count)건이 사라집니다. GitHub 에 올라간 리뷰와 처리 지문은 그대로라, 지웠다고 다시 리뷰하지는 않습니다.")
        }
    }

    // MARK: 계정

    private var account: some View {
        section("계정과 저장소") {
            ForEach(store.status?.accounts ?? []) { a in
                HStack(spacing: 6) {
                    Image(systemName: a.ok ? "checkmark.seal" : "xmark.seal")
                        .foregroundStyle(a.ok ? Color.green : .red)
                    Text(a.account ?? "(전역 gh)").f(.callout)
                    if let owner = a.tokenOwner {
                        Text("토큰 주인 \(owner)").f(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Text(a.repos.joined(separator: ", ")).f(.caption).foregroundStyle(.secondary)
                }
                if let err = a.error {
                    Text(err).f(.caption).foregroundStyle(.red)
                }
            }
            HStack(spacing: 6) {
                TextField("gh 계정 이름", text: $newAccount).textFieldStyle(.roundedBorder)
                    .frame(width: 190)
                Button("설정") {
                    let n = newAccount.trimmingCharacters(in: .whitespaces)
                    Task { await store.setAccount(n); newAccount = "" }
                }
                .disabled(newAccount.trimmingCharacters(in: .whitespaces).isEmpty || store.busy != nil)
                Button("지정 해제") { Task { await store.clearAccount() } }
                    .disabled(store.busy != nil)
            }
            Text("설정한 계정의 토큰 주인이 일치할 때만 받아들입니다. 지정을 해제하면 그때그때 활성인 gh 계정을 따릅니다 — 계정이 둘 이상이면 반드시 지정하세요.")
                .f(.caption).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            Text(BotPaths.root.path).f(.caption2, mono: true)
                .foregroundStyle(.tertiary).textSelection(.enabled)
        }
    }

    // MARK: 조각

    // MARK: 저장소

    private var repositories: some View {
        section("감시 저장소") {
            Text("여기 없는 저장소의 PR 은 건드리지 않습니다 — 이 목록이 곧 안전장치입니다.")
                .f(.caption).foregroundStyle(.secondary)
            ForEach(store.status?.repoDetails ?? []) { r in
                HStack(spacing: 8) {
                    Toggle("", isOn: Binding(
                        get: { r.enabled },
                        set: { on in Task { await store.setRepoEnabled(r.slug, on) } }))
                        .labelsHidden().toggleStyle(.switch).controlSize(.mini)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(r.slug).f(.callout, .semibold)
                            .foregroundStyle(r.enabled ? .primary : .secondary)
                        if !r.localPath.isEmpty {
                            HStack(spacing: 4) {
                                Image(systemName: r.cloned ? "folder" : "exclamationmark.triangle")
                                    .foregroundStyle(r.cloned ? Color.secondary : .orange)
                                Text(r.localPath).f(.caption2, mono: true)
                                    .foregroundStyle(.secondary)
                            }
                        }
                        if !r.account.isEmpty {
                            Text("계정 \(r.account)").f(.caption2).foregroundStyle(.secondary)
                        }
                    }
                    Spacer()
                    Button(role: .destructive) { confirmRemoveRepo = r.slug } label: {
                        Image(systemName: "minus.circle")
                    }.buttonStyle(.borderless)
                }
                .padding(10)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(.quaternary.opacity(0.3), in: RoundedRectangle(cornerRadius: 8))
                .disabled(store.busy != nil)
            }
            if (store.status?.repoDetails ?? []).isEmpty {
                Text("감시 저장소가 없습니다 — 아무 PR 도 리뷰하지 않습니다.")
                    .f(.caption).foregroundStyle(.orange)
            }
            HStack(spacing: 6) {
                TextField("owner/repo", text: $newRepo).textFieldStyle(.roundedBorder)
                    .frame(width: 190)
                TextField("로컬 클론 경로 (선택)", text: $newRepoPath)
                    .textFieldStyle(.roundedBorder)
                Button("찾기…") { chooseRepoPath() }.f(.caption)
                Button("추가") {
                    let slug = newRepo.trimmingCharacters(in: .whitespaces)
                    let path = newRepoPath.trimmingCharacters(in: .whitespaces)
                    Task {
                        await store.addRepo(slug, path: path, account: "")
                        newRepo = ""; newRepoPath = ""
                    }
                }
                .disabled(!newRepo.contains("/") || store.busy != nil)
            }
            .alert("저장소를 해제할까요?", isPresented: Binding(
                get: { confirmRemoveRepo != nil },
                set: { if !$0 { confirmRemoveRepo = nil } })) {
                Button("해제", role: .destructive) {
                    if let r = confirmRemoveRepo { Task { await store.removeRepo(r) } }
                    confirmRemoveRepo = nil
                }
                Button("취소", role: .cancel) { confirmRemoveRepo = nil }
            } message: {
                Text("\(confirmRemoveRepo ?? "") 의 PR 을 더 이상 리뷰하지 않습니다.")
            }
        }
    }

    // MARK: 파일 고르기

    private func chooseAgentFile() {
        let panel = NSOpenPanel()
        panel.allowedContentTypes = [UTType(filenameExtension: "md") ?? .plainText]
        panel.allowsMultipleSelection = false
        panel.message = "에이전트 정의(.md)를 고르세요"
        if panel.runModal() == .OK, let url = panel.url {
            Task { await store.addAgent(path: url.path) }
        }
    }

    private func chooseRepoPath() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.message = "저장소의 로컬 클론 폴더를 고르세요"
        if panel.runModal() == .OK, let url = panel.url { newRepoPath = url.path }
    }

    private func section(_ title: String, @ViewBuilder content: () -> some View) -> some View {
        VStack(alignment: .leading, spacing: 9) {
            Text(title).f(.headline)
            content()
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func row(_ k: String, _ v: String) -> some View {
        HStack(alignment: .top) {
            Text(k).f(.callout).foregroundStyle(.secondary)
            Spacer()
            Text(v).f(.callout, digits: true)
        }
    }
}

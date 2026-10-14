import SwiftUI

// MARK: - 리뷰 한 건

struct RunDetailView: View {
    @Environment(BotStore.self) private var store
    let run: RunRecord
    @State private var draft: String?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                head
                if run.wasSkipped { skippedBox }
                if run.failed { failureBox }
                if run.partial { partialBox }
                if let decision = run.decision { decisionBox(decision) }
                else if let reason = run.reason, !reason.isEmpty { verdictBox(reason) }
                if let summary = run.summary, !summary.isEmpty {
                    section("리뷰어 요약") { Markdown(text: summary) }
                }
                if let draft { section("초안") { Markdown(text: draft) } }
                meta
            }
            .padding(20)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .defaultScrollAnchor(.top)
        .task(id: run.id) { draft = loadDraft() }
    }

    private var head: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                Text(run.prLabel).f(.title2, .semibold)
                if run.wasSkipped { Text("건너뜀").f(.caption).foregroundStyle(.orange) }
                else if run.failed { Text("실패").f(.caption).foregroundStyle(.red) }
                else { EventBadge(event: run.event) }
                if run.degraded == true {
                    Text("강등 게시").f(.caption2).foregroundStyle(.orange)
                }
                Spacer()
                if let repo = run.repo, let n = run.number {
                    Button {
                        if let url = URL(string: "https://github.com/\(repo)/pull/\(n)") {
                            NSWorkspace.shared.open(url)
                        }
                    } label: { Label("GitHub 에서 열기", systemImage: "arrow.up.forward.square") }
                        .buttonStyle(.bordered).controlSize(.small)
                }
            }
            if let t = run.title, !t.isEmpty {
                Text(t).f(.callout).foregroundStyle(.secondary)
            }
            HStack(spacing: 12) {
                stat("인라인", "\(run.inline ?? 0)")
                stat("본문", "\(run.demoted ?? 0)")
                stat("버려짐", "\(run.dropped ?? 0)")
                if let l = run.lostAnchors { stat("앵커 유실", "\(l)") }
                stat("게시", (run.posted ?? false) ? "예" : "아니오")
            }
            .padding(.top, 2)
        }
    }

    private func stat(_ label: String, _ value: String) -> some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(label).f(.caption2).foregroundStyle(.secondary)
            Text(value).f(.callout, .medium, digits: true)
        }
    }

    private var skippedBox: some View {
        box(.orange, "너무 커서 리뷰하지 않았습니다",
            (run.skipped ?? "")
            + "\n\n터미널에서 `review-bot review \(run.repo ?? "")#\(run.number ?? 0) --force-size` "
            + "로 상한을 무시하고 볼 수 있습니다. 그 경우 diff 가 프롬프트에 다 들어가지 "
            + "못하면 일부 파일은 리뷰어가 워크트리에서 직접 읽습니다.")
    }

    private var failureBox: some View {
        box(.red, "리뷰가 실패했습니다", run.error ?? "")
    }

    private var partialBox: some View {
        box(.orange, "리뷰어 실행 오류 — 보완 여부는 최종 판정을 확인하세요",
            (run.agentErrors ?? []).joined(separator: "\n"))
    }

    private func verdictBox(_ reason: String) -> some View {
        box(.blue, "판정", reason)
    }

    private func decisionBox(_ decision: DecisionRecord) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 6) {
                Text("AI 판정").f(.caption)
                if let event = decision.judgeEvent { EventBadge(event: event) }
                else { Text("미실행").f(.caption).foregroundStyle(.secondary) }
                Image(systemName: "arrow.right").foregroundStyle(.secondary)
                Text("최종").f(.caption)
                EventBadge(event: decision.finalEvent ?? run.event)
            }
            if let summary = decision.summary, !summary.isEmpty {
                Text(summary).f(.callout).textSelection(.enabled)
            }
            ForEach(decision.reasons ?? []) { reason in
                VStack(alignment: .leading, spacing: 3) {
                    Text(reason.label).f(.caption, .semibold)
                    Text(reason.message).f(.callout).textSelection(.enabled)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(12)
        .background(.blue.opacity(0.08), in: RoundedRectangle(cornerRadius: 8))
    }

    private func box(_ color: Color, _ title: String, _ body: String) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(title).f(.caption, .semibold).foregroundStyle(color)
            Text(body).f(.callout).textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(12)
        .background(color.opacity(0.08), in: RoundedRectangle(cornerRadius: 8))
        .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(color.opacity(0.25)))
    }

    private func section(_ title: String, @ViewBuilder content: () -> some View) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            Text(title).f(.caption, .semibold).foregroundStyle(.secondary)
            content().frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var meta: some View {
        VStack(alignment: .leading, spacing: 3) {
            Divider().padding(.vertical, 4)
            metaRow("시각", run.at ?? "-")
            metaRow("계정", run.account ?? "-")
            metaRow("커밋", String((run.sha ?? "-").prefix(12)))
            if let d = run.reviewDecision { metaRow("PR 유효 판정", d.isEmpty ? "없음" : d) }
            if let p = run.postError { metaRow("게시 오류", p) }
            if let d = run.draft { metaRow("초안 파일", d) }
        }
    }

    private func metaRow(_ k: String, _ v: String) -> some View {
        HStack(alignment: .top, spacing: 8) {
            Text(k).f(.caption).foregroundStyle(.secondary).frame(width: 90, alignment: .leading)
            Text(v).f(.caption, mono: true).textSelection(.enabled)
        }
    }

    private func loadDraft() -> String? {
        guard let path = run.draft,
              let text = try? String(contentsOfFile: path, encoding: .utf8) else { return nil }
        return text
    }
}

// MARK: - 살아 있는 세션

struct LiveDetailView: View {
    @Environment(BotStore.self) private var store
    let session: LiveSession

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    Text(session.agent).f(.title2, .semibold)
                    Text(session.pr).f(.callout).foregroundStyle(.secondary)
                }
                if let c = store.current { subject(c) }
                HStack(spacing: 16) {
                    VStack(alignment: .leading) {
                        Text("턴").f(.caption2).foregroundStyle(.secondary)
                        Text(String(session.turns)).f(.title3, digits: true)
                    }
                    VStack(alignment: .leading) {
                        Text("마지막 활동").f(.caption2).foregroundStyle(.secondary)
                        Text("\(session.idleSeconds)초 전").f(.title3, digits: true)
                    }
                    if session.deniedTools > 0 {
                        VStack(alignment: .leading) {
                            Text("거절된 툴 호출").f(.caption2).foregroundStyle(.secondary)
                            Text("\(session.deniedTools)").f(.title3, digits: true)
                                .foregroundStyle(.orange)
                        }
                    }
                }
                VStack(alignment: .leading, spacing: 7) {
                    Text("툴 사용").f(.caption, .semibold).foregroundStyle(.secondary)
                    ForEach(session.tools.sorted(by: { $0.value > $1.value }), id: \.key) { name, n in
                        HStack {
                            Text(name).f(.callout, mono: true)
                            Spacer()
                            Text("\(n)").f(.callout, digits: true).foregroundStyle(.secondary)
                        }
                    }
                }
                if !session.lastDetail.isEmpty {
                    VStack(alignment: .leading, spacing: 5) {
                        Text("지금 보고 있는 것").f(.caption, .semibold)
                            .foregroundStyle(.secondary)
                        Text("\(session.lastTool) · \(session.lastDetail)")
                            .f(.callout, mono: true).textSelection(.enabled)
                    }
                }
                Text("세션 파일: \(session.id)")
                    .f(.caption2, mono: true).foregroundStyle(.tertiary).textSelection(.enabled)
            }
            .padding(20)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .defaultScrollAnchor(.top)
    }

    /// 지금 무엇을 보고 있는가 — 봇이 리뷰를 시작하며 남긴 state/current.json 에서 온다.
    /// 워크트리 경로에는 저장소와 번호밖에 없어서 작성자·요청자는 거기서만 나온다.
    @ViewBuilder
    private func subject(_ c: CurrentRun) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            if let t = c.title, !t.isEmpty {
                Text(t).f(.callout, .medium).lineLimit(2)
            }
            HStack(spacing: 14) {
                Label(c.author ?? "?", systemImage: "person")
                Label(c.requestedBy ?? "요청자 확인 안 됨", systemImage: "person.badge.plus")
                if let f = c.files, let ch = c.churn {
                    Label("파일 \(f) · \(ch)줄", systemImage: "doc.text")
                }
                if let sha = c.sha {
                    Label(String(sha.prefix(7)), systemImage: "number")
                }
            }
            .f(.caption).foregroundStyle(.secondary)
            if let coverage = c.coverage {
                Text("\(coverage.batches)묶음 · 묶음 검토 \(coverage.completed)/\(coverage.planned) · 통합 검토 \(coverage.integrationCompleted)/\(coverage.integrationPlanned)")
                    .f(.caption, digits: true).foregroundStyle(.secondary)
            }
            HStack(spacing: 14) {
                if let started = c.started {
                    TimelineView(.periodic(from: .now, by: 1)) { _ in
                        let secs = max(0, Int(Date().timeIntervalSince(started)))
                        Label(String(format: "%d분 %02d초째", secs / 60, secs % 60),
                              systemImage: "timer").f(.caption, digits: true)
                    }
                }
                if let agents = c.agents, !agents.isEmpty {
                    Label(agents.joined(separator: " → "), systemImage: "person.2").f(.caption)
                }
                Spacer()
                if let url = c.url {
                    Button {
                        if let u = URL(string: url) { NSWorkspace.shared.open(u) }
                    } label: { Label("PR 열기", systemImage: "arrow.up.forward.square").f(.caption) }
                        .buttonStyle(.borderless)
                }
            }
            .foregroundStyle(.secondary)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.quaternary.opacity(0.3), in: RoundedRectangle(cornerRadius: 8))
    }
}

// MARK: - 대기 중인 PR

struct PendingDetailView: View {
    @Environment(BotStore.self) private var store
    let pr: PendingPR
    @State private var confirming = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                VStack(alignment: .leading, spacing: 6) {
                    HStack(spacing: 10) {
                        PRNumberBadge(number: pr.number)
                        if pr.isDraft == true { DraftBadge(reason: pr.holdReason) }
                        Text(pr.repo).f(.callout).foregroundStyle(.secondary)
                    }
                    Text(pr.title).f(.callout).foregroundStyle(.secondary)
                    HStack(spacing: 10) {
                        Label(pr.prAuthor ?? pr.author, systemImage: "person")
                        if let n = pr.changedFiles { Label("파일 \(n)", systemImage: "doc") }
                        Label(shortTime(pr.updatedAt.replacingOccurrences(of: "Z", with: "")),
                              systemImage: "clock")
                        if pr.status == "new", let hold = pr.holdReason {
                            Label(statusText, systemImage: "pause.circle").foregroundStyle(.orange).help(hold)
                        } else {
                            Text(statusText).foregroundStyle(.orange)
                        }
                    }
                    .f(.caption).foregroundStyle(.secondary)
                }

                people
                if let insight = pr.insight { InsightDetailView(insight: insight) }

                HStack {
                    Button {
                        if let url = URL(string: pr.url) { NSWorkspace.shared.open(url) }
                    } label: { Label("GitHub 에서 열기", systemImage: "arrow.up.forward.square") }
                    Button {
                        confirming = true
                    } label: { Label("지금 재리뷰", systemImage: "sparkle.magnifyingglass") }
                        .disabled(store.busy != nil || store.health == .running || !pr.watched)
                }
                .buttonStyle(.bordered)

                if let rec = store.reviewed["\(pr.repo)#\(pr.number)"] {
                    VStack(alignment: .leading, spacing: 3) {
                        Text("마지막 처리").f(.caption, .semibold).foregroundStyle(.secondary)
                        Text("\(rec.at ?? "-") · \(rec.event ?? "-") · 인라인 \(rec.inline ?? 0) · \(rec.event == "DISCARDED" ? "로컬 자료 정리됨" : rec.event == "PENDING" ? "검증 대기" : (rec.posted ?? false) ? "게시함" : "초안만")")
                            .f(.caption, mono: true)
                        Text("커밋 \(String((rec.sha ?? "-").prefix(12)))")
                            .f(.caption, mono: true).foregroundStyle(.secondary)
                    }
                }

                if !store.lastCommandOutput.isEmpty {
                    Text(store.lastCommandOutput)
                        .f(.caption, mono: true).textSelection(.enabled)
                        .padding(10)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 6))
                }
            }
            .padding(20)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .confirmationDialog("이 PR 을 다시 리뷰할까요?", isPresented: $confirming) {
            Button("재리뷰 실행", role: .destructive) {
                Task { await store.review(pr.repo, pr.number) }
            }
            Button("취소", role: .cancel) {}
        } message: {
            Text("리뷰어 두 개가 처음부터 다시 돌고, 게시 모드면 결과가 실제로 GitHub 에 올라갑니다.")
        }
    }

    /// 누가 나를 불렀고, 이 PR 이 어느 이슈에서 나왔는가.
    private var people: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Image(systemName: "person.badge.plus").foregroundStyle(.secondary)
                Text("리뷰 요청").f(.caption).foregroundStyle(.secondary)
                Text(pr.requestedBy ?? "확인되지 않음")
                    .f(.callout, .medium)
                Text("→ 나").f(.caption).foregroundStyle(.tertiary)
            }
            if let issues = pr.issues, !issues.isEmpty {
                ForEach(issues) { issue in
                    HStack(alignment: .top, spacing: 8) {
                        Image(systemName: issue.state == "open"
                              ? "smallcircle.filled.circle" : "checkmark.circle")
                            .foregroundStyle(issue.state == "open" ? Color.green : .purple)
                        VStack(alignment: .leading, spacing: 2) {
                            HStack(spacing: 6) {
                                Text("#" + String(issue.number)).f(.callout, .medium)
                                Text(issue.title).f(.callout).lineLimit(2)
                            }
                            Text("발행 \(issue.author)").f(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        Button {
                            if let url = URL(string: issue.url) { NSWorkspace.shared.open(url) }
                        } label: { Image(systemName: "arrow.up.forward.square") }
                            .buttonStyle(.borderless)
                    }
                }
            } else {
                Text("연결된 이슈를 찾지 못했습니다 (제목·본문의 #번호 를 따라갑니다)")
                    .f(.caption).foregroundStyle(.tertiary)
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.quaternary.opacity(0.3), in: RoundedRectangle(cornerRadius: 8))
    }

    private var statusText: String {
        if pr.status == "new", pr.holdReason != nil { return "자동 리뷰 보류" }
        return switch pr.status {
        case "posted": "리뷰 게시함"
        case "pending": "검증 대기"
        case "discarded": "로컬 자료 정리됨"
        case "draft": "초안만 있음 — 다음 틱에 재리뷰"
        case "new": "아직 리뷰 안 함"
        default: "감시 대상 아님"
        }
    }
}

// MARK: - 로그

struct LogPaneView: View {
    @Environment(BotStore.self) private var store
    @State private var showPicker = false

    /// 날짜 줄. 기본은 오늘만 보여 주고, 지난 날은 고를 때만 읽는다 — 로그를
    /// 한 파일에 쌓아 두면 어제 것이 오늘 것처럼 보인다.
    private var dayBar: some View {
        HStack(spacing: 8) {
            Text(store.logDay == BotPaths.today ? "오늘" : store.logDay)
                .f(.caption, .semibold)
            if store.logDay != BotPaths.today {
                Button("오늘로") { store.showLogDay(BotPaths.today) }.f(.caption2)
            }
            Spacer()
            let earlier = store.logDays.filter { $0 != store.logDay }
            if !earlier.isEmpty {
                Menu {
                    ForEach(earlier, id: \.self) { d in
                        Button(d == BotPaths.today ? "오늘" : d) { store.showLogDay(d) }
                    }
                } label: {
                    Label("지난 로그 \(earlier.count)일", systemImage: "calendar")
                        .f(.caption2)
                }
                .menuStyle(.borderlessButton).fixedSize()
            }
        }
        .padding(.horizontal, 16).padding(.vertical, 8)
        .background(.quaternary.opacity(0.25))
    }

    var body: some View {
        VStack(spacing: 0) {
        dayBar
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 2) {
                    ForEach(store.logLines) { line in
                        HStack(alignment: .top, spacing: 8) {
                            Text(line.time)
                                .f(.caption, digits: true).foregroundStyle(.tertiary)
                            Text(line.text)
                                .f(.caption, mono: true)
                                .foregroundStyle(line.isError ? Color.red : .primary)
                                .padding(.leading, line.indented ? 12 : 0)
                                .textSelection(.enabled)
                            Spacer(minLength: 0)
                        }
                        .id(line.id)
                    }
                }
                .padding(16)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .onChange(of: store.logLines.count) {
                if let last = store.logLines.last { proxy.scrollTo(last.id, anchor: .bottom) }
            }
            .onAppear {
                if let last = store.logLines.last { proxy.scrollTo(last.id, anchor: .bottom) }
            }
        }
        .overlay(alignment: .topTrailing) {
            if let err = store.statusError {
                Text(err).f(.caption).padding(8)
                    .background(.red.opacity(0.15), in: RoundedRectangle(cornerRadius: 6))
                    .padding(10)
            }
        }
        }
    }
}

// MARK: - 게시를 기다리는 초안

struct DraftDetailView: View {
    @Environment(BotStore.self) private var store
    let draft: DraftPayload
    @State private var confirming = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                head
                actions
                if let out = commandOutput { outputBox(out) }
                if let reason = draft.reason, !reason.isEmpty {
                    labeled("판정") {
                        Text(reason).f(.callout).textSelection(.enabled)
                    }
                }
                if let body = draft.body, !body.isEmpty {
                    labeled("올라갈 요약 본문") {
                        Markdown(text: body)
                            .padding(12)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .background(.quaternary.opacity(0.3),
                                        in: RoundedRectangle(cornerRadius: 8))
                    }
                }
                if let comments = draft.comments, !comments.isEmpty {
                    labeled("인라인 \(comments.count)건") {
                        VStack(alignment: .leading, spacing: 10) {
                            ForEach(comments) { c in
                                VStack(alignment: .leading, spacing: 6) {
                                    Text("\(c.path):\(c.line.map(String.init) ?? "?")")
                                        .f(.caption, mono: true).foregroundStyle(.secondary)
                                    if let hunk = c.hunk, !hunk.isEmpty {
                                        DiffHunkView(lines: hunk, focus: c.line)
                                    }
                                    Markdown(text: c.body)
                                }
                                .padding(10)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .background(.quaternary.opacity(0.3),
                                            in: RoundedRectangle(cornerRadius: 8))
                            }
                        }
                    }
                }
                if let dropped = draft.dropped, !dropped.isEmpty {
                    labeled("판정이 버린 지적 \(dropped.count)건") {
                        VStack(alignment: .leading, spacing: 3) {
                            ForEach(dropped) { d in
                                Text("· [\(d.severity ?? "?")] \(d.title ?? "")")
                                    .f(.caption).foregroundStyle(.secondary)
                            }
                        }
                    }
                }
            }
            .padding(20)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .defaultScrollAnchor(.top)
        .confirmationDialog(publishQuestion, isPresented: $confirming) {
            Button(publishLabel, role: .destructive) { Task { await store.publish(draft) } }
            Button("취소", role: .cancel) {}
        } message: {
            Text("\(publishConsequence)\n\n여기 보이는 그대로 \(draft.repo)#\(String(draft.number)) 에 올라갑니다. 리뷰를 다시 돌리지는 않습니다. 초안을 만든 뒤 새 커밋이 올라왔다면 봇이 게시를 거절합니다.")
        }
    }

    private var head: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                Text(draft.label).f(.title2, .semibold)
                EventBadge(event: draft.event)
                Text("게시 대기").f(.caption2)
                    .padding(.horizontal, 6).padding(.vertical, 2)
                    .background(.orange.opacity(0.15), in: Capsule())
                    .foregroundStyle(.orange)
            }
            if let t = draft.title, !t.isEmpty {
                Text(t).f(.callout).foregroundStyle(.secondary)
            }
            Text("인라인 \(draft.inline ?? 0) · 본문 \(draft.demoted ?? 0) · \(draft.at ?? "-") · 커밋 \(String((draft.sha ?? "-").prefix(12)))")
                .f(.caption, mono: true).foregroundStyle(.secondary)
        }
    }

    // 게시는 되돌릴 수 없고, 승인·변경요청은 사용자 이름으로 나가는 상태 변경이다.
    // 버튼과 확인 문구가 어느 쪽인지 분명히 말해야 한다.
    private var publishLabel: String {
        switch draft.event {
        case "APPROVE": "승인으로 게시"
        case "REQUEST_CHANGES": "변경 요청으로 게시"
        default: "코멘트로 게시"
        }
    }
    private var publishIcon: String {
        switch draft.event {
        case "APPROVE": "checkmark.seal.fill"
        case "REQUEST_CHANGES": "exclamationmark.octagon.fill"
        default: "paperplane.fill"
        }
    }
    private var publishQuestion: String {
        switch draft.event {
        case "APPROVE": "이 PR 을 승인(Approve)할까요?"
        case "REQUEST_CHANGES": "이 PR 에 변경 요청(Request changes)을 낼까요?"
        default: "이 초안을 코멘트로 올릴까요?"
        }
    }
    private var publishConsequence: String {
        switch draft.event {
        case "APPROVE":
            "✅ 승인입니다 — 내 이름의 Approve 로 기록되고, branch protection 의 required approval 을 채웁니다."
        case "REQUEST_CHANGES":
            "🛑 변경 요청입니다 — PR 이 차단 상태가 되고, 작성자가 반영한 뒤 직접 해제해야 머지됩니다."
        default:
            "💬 코멘트입니다 — 머지를 막지도, 승인하지도 않습니다."
        }
    }

    private var actions: some View {
        HStack {
            Button { confirming = true } label: {
                Label(publishLabel, systemImage: publishIcon)
            }
            .buttonStyle(.borderedProminent)
            .disabled(store.busy != nil || store.health == .running)

            Button {
                if let url = URL(string: "https://github.com/\(draft.repo)/pull/\(draft.number)") {
                    NSWorkspace.shared.open(url)
                }
            } label: { Label("PR 열기", systemImage: "arrow.up.forward.square") }

            Button {
                Task { await store.review(draft.repo, draft.number) }
            } label: { Label("다시 리뷰", systemImage: "arrow.clockwise") }
                .disabled(store.busy != nil || store.health == .running)

            if store.busy != nil { ProgressView().controlSize(.small) }
        }
        .buttonStyle(.bordered)
    }

    /// 게시가 거절되면 그 이유가 여기로 온다 — 새 커밋이 올라온 경우가 대표적이다.
    private var commandOutput: String? {
        let out = store.lastCommandOutput
        return out.isEmpty ? nil : out
    }

    private func outputBox(_ text: String) -> some View {
        Text(text)
            .f(.caption, mono: true).textSelection(.enabled)
            .padding(10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(text.contains("게시하지 않습니다") || text.contains("실패")
                        ? Color.red.opacity(0.1) : Color.green.opacity(0.1),
                        in: RoundedRectangle(cornerRadius: 6))
    }

    private func labeled(_ title: String, @ViewBuilder content: () -> some View) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            Text(title).f(.caption, .semibold).foregroundStyle(.secondary)
            content().frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

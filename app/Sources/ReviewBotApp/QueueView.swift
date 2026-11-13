import SwiftUI

/// 봇이 볼 순서를 한 줄로 세운 것 — 지금 보는 PR, 사람이 요청해 줄 선 PR, 그 뒤에
/// 틱이 알아서 집어 갈 PR. 앞의 둘은 봇이 지키는 순서이고, 마지막은 예상이다.
struct QueueRow: Identifiable, Equatable, Sendable {
    enum Kind: Equatable, Sendable {
        case running
        case requested(position: Int)
        case automatic
    }

    var repo: String
    var number: Int
    var title: String?
    var kind: Kind

    var id: String { "\(repo)#\(number)" }
    var label: String { "\(repo.split(separator: "/").last.map(String.init) ?? repo)#\(number)" }

    /// `titles` 는 PR id → 제목. 대기열 파일에는 저장소와 번호뿐이라 초안·요청 목록에서 찾아 온다.
    static func build(current: CurrentRun?, running: Bool, queue: [QueuedReview],
                      pending: [PendingPR], titles: [String: String] = [:]) -> [QueueRow] {
        var rows: [QueueRow] = []
        var seen: Set<String> = []
        func title(_ repo: String, _ number: Int, _ own: String? = nil) -> String? {
            if let own, !own.isEmpty { return own }
            return titles["\(repo)#\(number)"]
        }
        if running, let c = current {
            rows.append(QueueRow(repo: c.repo, number: c.number, title: title(c.repo, c.number, c.title), kind: .running))
            seen.insert("\(c.repo)#\(c.number)")
        }
        for (index, item) in queue.enumerated() {
            // 지금 보는 PR 을 다시 요청해 둔 경우도 줄에는 그대로 보인다 — 끝나면 한 번 더 돈다.
            rows.append(QueueRow(repo: item.repo, number: item.number,
                                 title: title(item.repo, item.number), kind: .requested(position: index + 1)))
            seen.insert("\(item.repo)#\(item.number)")
        }
        // 틱이 다음에 집어 갈 것: 감시 중이고, 아직 리뷰하지 않았고, 보류 사유가 없는 요청.
        for pr in pending where pr.watched && pr.status == "new" && pr.holdReason == nil && !seen.contains(pr.id) {
            rows.append(QueueRow(repo: pr.repo, number: pr.number, title: pr.title, kind: .automatic))
        }
        return rows
    }
}

/// 사이드바의 "작업 대기열". 봇이 일하고 있거나 줄 선 요청이 있을 때만 보인다.
struct ReviewQueueSection: View {
    @Environment(BotStore.self) private var store
    @Binding var selection: Selection?
    @State private var hovered: String?

    private var rows: [QueueRow] {
        var titles: [String: String] = [:]
        for p in store.status?.pending ?? [] { titles[p.id] = p.title }
        for d in store.drafts { if let t = d.title, !t.isEmpty { titles[d.id] = t } }
        return QueueRow.build(current: store.current, running: store.health == .running,
                              queue: store.queue, pending: store.status?.pending ?? [], titles: titles)
    }

    static func visible(running: Bool, queue: [QueuedReview]) -> Bool { running || !queue.isEmpty }

    var body: some View {
        let rows = rows
        if Self.visible(running: store.health == .running, queue: store.queue), !rows.isEmpty {
            VStack(alignment: .leading, spacing: 0) {
                HStack {
                    Text("작업 대기열").f(.caption2, .semibold).foregroundStyle(.secondary)
                    Spacer()
                    if !store.queue.isEmpty {
                        Text("요청 \(store.queue.count)건").f(.caption2, digits: true).foregroundStyle(.secondary)
                    }
                }
                .padding(.horizontal, 12).padding(.bottom, 8)
                ForEach(Array(rows.enumerated()), id: \.offset) { index, row in
                    rowView(row, isLast: index == rows.count - 1)
                }
            }
        }
    }

    private func rowView(_ row: QueueRow, isLast: Bool) -> some View {
        HStack(alignment: .top, spacing: 8) {
            // 왼쪽 레일: 순서를 잇는 선과 각 PR 의 표지. 예상 구간은 점선으로 구분한다.
            VStack(spacing: 0) {
                marker(row).frame(width: 18, height: 18)
                if !isLast {
                    Rectangle()
                        .fill(row.kind == .automatic ? Color.clear : Color.secondary.opacity(0.35))
                        .overlay {
                            if row.kind == .automatic {
                                Line().stroke(Color.secondary.opacity(0.35), style: StrokeStyle(lineWidth: 1, dash: [2, 3]))
                            }
                        }
                        .frame(width: 1).frame(maxHeight: .infinity)
                }
            }
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 6) {
                    Text(row.label)
                        .f(.callout, row.kind == .running ? .semibold : .regular)
                        .foregroundStyle(row.kind == .automatic ? .secondary : .primary)
                    tag(row)
                    Spacer(minLength: 0)
                    controls(row)
                }
                if let title = row.title, !title.isEmpty {
                    Text(title).f(.caption2).foregroundStyle(.secondary).lineLimit(1)
                }
                if row.kind == .running { progress }
            }
            .padding(.bottom, isLast ? 0 : 10)
        }
        .padding(.horizontal, 12).padding(.vertical, 2)
        .contentShape(Rectangle())
        .onHover { hovered = $0 ? row.id + "\(row.kind)" : nil }
        .onTapGesture { open(row) }
        .help(help(row))
    }

    @ViewBuilder private func marker(_ row: QueueRow) -> some View {
        switch row.kind {
        case .running:
            ZStack {
                Circle().fill(Color.accentColor.opacity(0.18))
                ProgressView().controlSize(.mini)
            }
        case .requested(let position):
            ZStack {
                Circle().fill(Color.accentColor)
                Text("\(position)").f(.caption2, .semibold, digits: true).foregroundStyle(.white)
            }
        case .automatic:
            Circle().strokeBorder(Color.secondary.opacity(0.5), style: StrokeStyle(lineWidth: 1, dash: [2, 2]))
                .padding(3)
        }
    }

    @ViewBuilder private func tag(_ row: QueueRow) -> some View {
        switch row.kind {
        case .running:
            Text("리뷰 중").f(.caption2, .medium).foregroundStyle(Color.accentColor)
        case .requested:
            Text("요청").f(.caption2).foregroundStyle(.secondary)
        case .automatic:
            Text("자동").f(.caption2).foregroundStyle(.tertiary)
        }
    }

    /// 줄을 바꾸는 단추는 올려 둔 행에만 보인다 — 늘 보이면 좁은 사이드바가 단추로 찬다.
    @ViewBuilder private func controls(_ row: QueueRow) -> some View {
        if hovered == row.id + "\(row.kind)" {
            HStack(spacing: 2) {
                switch row.kind {
                case .running:
                    EmptyView()
                case .requested(let position):
                    if position > 1 {
                        Button { Task { await store.prioritizeQueued(row.repo, row.number) } } label: {
                            Image(systemName: "arrow.up.to.line")
                        }.help("맨 앞으로")
                    }
                    Button { Task { await store.cancelQueued(row.repo, row.number) } } label: {
                        Image(systemName: "xmark")
                    }.help("대기열에서 빼기")
                case .automatic:
                    Button { Task { await store.review(row.repo, row.number) } } label: {
                        Image(systemName: "arrow.up.to.line")
                    }.help("먼저 보기 — 요청으로 줄 세웁니다")
                }
            }
            .buttonStyle(.borderless).controlSize(.small)
            .disabled(store.busy != nil)
        }
    }

    /// 지금 보는 PR 의 진행: 어느 단계인지와 얼마나 됐는지.
    private var progress: some View {
        HStack(spacing: 6) {
            if let phase = store.current?.phase, !phase.isEmpty {
                Text(phase).f(.caption2).foregroundStyle(Color.accentColor).lineLimit(1)
            }
            Spacer(minLength: 0)
            if let started = store.current?.started {
                TimelineView(.periodic(from: .now, by: 1)) { _ in
                    let s = max(0, Int(Date.now.timeIntervalSince(started)))
                    Text("\(s / 60):\(String(format: "%02d", s % 60))")
                        .f(.caption2, digits: true).foregroundStyle(.secondary)
                }
            }
        }
    }

    private func help(_ row: QueueRow) -> String {
        switch row.kind {
        case .running: "지금 리뷰하는 PR"
        case .requested(let position): "직접 요청한 리뷰 — \(position)번째. 하던 PR 이 끝나면 이 순서대로 봅니다"
        case .automatic: "틱이 알아서 볼 리뷰 요청 — 요청한 것들이 끝난 뒤에 봅니다 (예상 순서)"
        }
    }

    private func open(_ row: QueueRow) {
        if store.drafts.contains(where: { $0.id == row.id }) { selection = .draft(row.id) }
        else if store.status?.pending.contains(where: { $0.id == row.id }) == true { selection = .pending(row.id) }
    }
}

private struct Line: Shape {
    func path(in rect: CGRect) -> Path {
        var p = Path()
        p.move(to: CGPoint(x: rect.midX, y: rect.minY))
        p.addLine(to: CGPoint(x: rect.midX, y: rect.maxY))
        return p
    }
}

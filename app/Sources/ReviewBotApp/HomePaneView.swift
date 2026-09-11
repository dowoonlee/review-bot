import SwiftUI

/// 홈 — 지금 무엇이 벌어지고 있는지 한 화면에서 본다.
///
/// 다른 화면은 전부 "무언가 하나"를 깊게 본다(초안 하나, 세션 하나, 로그 전부).
/// 처음 창을 열었을 때 필요한 것은 그게 아니라 "괜찮은가, 내가 볼 것이 있는가" 다.
struct HomePaneView: View {
    @Environment(BotStore.self) private var store
    @Binding var selection: Selection?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                nowCard
                if !store.drafts.isEmpty { draftsCard }
                todayCard
                pendingCard
                recentCard
            }
            .padding(20)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    // MARK: 지금

    private var nowCard: some View {
        let live = store.live
        return card {
            HStack(alignment: .top, spacing: 12) {
                if !live.isEmpty {
                    ProgressView().controlSize(.small)
                } else {
                    Image(systemName: store.paused != nil ? "pause.circle" : "checkmark.circle")
                        .f(.title2)
                        .foregroundStyle(store.paused != nil ? Color.orange : .green)
                }
                VStack(alignment: .leading, spacing: 4) {
                    if let c = store.current, !live.isEmpty {
                        Text("리뷰 중 — \(c.label)").f(.title3, .semibold)
                        Text(c.title ?? "").f(.callout).foregroundStyle(.secondary).lineLimit(2)
                        Text(live.map { "\($0.agent) 턴 \($0.turns)" }.joined(separator: " · "))
                            .f(.caption).foregroundStyle(.tertiary)
                    } else if store.paused != nil {
                        Text("일시정지").f(.title3, .semibold)
                        Text("`review-bot resume` 또는 설정에서 다시 켭니다")
                            .f(.callout).foregroundStyle(.secondary)
                    } else {
                        Text("대기 중").f(.title3, .semibold)
                        Text(nextTickText).f(.callout).foregroundStyle(.secondary)
                    }
                }
                Spacer()
                VStack(alignment: .trailing, spacing: 4) {
                    Text(store.autoPost ? "자동 게시" : "검토 후 게시")
                        .f(.caption, .semibold)
                        .padding(.horizontal, 8).padding(.vertical, 3)
                        .background(store.autoPost ? Color.accentColor.opacity(0.18)
                                                   : Color.orange.opacity(0.18),
                                    in: Capsule())
                    Button("지금 틱") { Task { await store.tickNow() } }
                        .f(.caption).disabled(store.busy != nil || !live.isEmpty)
                }
            }
        }
    }

    private var nextTickText: String {
        guard let sec = store.config?.intervalSec, sec > 0 else { return "자동 실행이 꺼져 있습니다" }
        return "\(sec / 60)분마다 리뷰 요청을 확인합니다"
    }

    // MARK: 게시를 기다리는 초안 — 가장 먼저 눈에 띄어야 한다

    private var draftsCard: some View {
        card(tint: .orange) {
            VStack(alignment: .leading, spacing: 8) {
                Label("검토를 기다리는 초안 \(store.drafts.count)건", systemImage: "tray.full")
                    .f(.callout, .semibold).foregroundStyle(.orange)
                ForEach(store.drafts.prefix(4)) { d in
                    Button { selection = .draft(d.id) } label: {
                        HStack(spacing: 6) {
                            Text(d.label).f(.callout)
                            EventBadge(event: d.event)
                            Spacer()
                            Image(systemName: "chevron.right").f(.caption2).foregroundStyle(.tertiary)
                        }
                    }
                    .buttonStyle(.plain)
                }
            }
        }
    }

    // MARK: 오늘

    private var todayCard: some View {
        let today = store.todaysRuns
        let posted = today.filter { $0.posted == true }.count
        let inline = today.reduce(0) { $0 + ($1.inline ?? 0) }
        let cost = today.reduce(0.0) { $0 + ($1.costUSD ?? 0) }
        return card {
            VStack(alignment: .leading, spacing: 10) {
                Text("오늘").f(.callout, .semibold)
                HStack(spacing: 26) {
                    stat("리뷰", "\(today.count)건")
                    stat("게시", "\(posted)건")
                    stat("인라인 지적", "\(inline)건")
                    if cost > 0 { stat("사용량", String(format: "$%.2f", cost)) }
                }
                if today.isEmpty {
                    Text("아직 처리한 PR 이 없습니다").f(.caption).foregroundStyle(.tertiary)
                }
            }
        }
    }

    private func stat(_ k: String, _ v: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(v).f(.title3, .semibold, digits: true)
            Text(k).f(.caption2).foregroundStyle(.secondary)
        }
    }

    // MARK: 리뷰 요청

    private var pendingCard: some View {
        let pending = store.status?.pending ?? []
        return card {
            VStack(alignment: .leading, spacing: 8) {
                Text("나에게 온 리뷰 요청 \(pending.count)건").f(.callout, .semibold)
                if pending.isEmpty {
                    Text("없습니다").f(.caption).foregroundStyle(.tertiary)
                }
                ForEach(pending.prefix(5)) { p in
                    Button { selection = .pending(p.id) } label: {
                        HStack(spacing: 6) {
                            Image(systemName: store.reviewed["\(p.repo)#\(p.number)"] != nil
                                  ? "checkmark.circle.fill" : "circle")
                                .f(.caption2)
                                .foregroundStyle(store.reviewed["\(p.repo)#\(p.number)"] != nil
                                                 ? AnyShapeStyle(Color.green) : AnyShapeStyle(.tertiary))
                            Text("\(p.repo)#\(p.number)").f(.caption, mono: true)
                            Text(p.title).f(.callout).lineLimit(1)
                            Spacer()
                            Image(systemName: "chevron.right").f(.caption2).foregroundStyle(.tertiary)
                        }
                    }
                    .buttonStyle(.plain)
                }
            }
        }
    }

    // MARK: 최근 활동

    private var recentCard: some View {
        card {
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    Text("최근 활동").f(.callout, .semibold)
                    Spacer()
                    Button("틱 로그") { selection = .log }.f(.caption)
                }
                if store.runs.isEmpty {
                    Text("기록이 없습니다").f(.caption).foregroundStyle(.tertiary)
                }
                ForEach(store.runs.prefix(6)) { r in
                    Button { selection = .run(r.id) } label: {
                        HStack(spacing: 6) {
                            Text((r.at ?? "").suffix(8)).f(.caption2, digits: true)
                                .foregroundStyle(.tertiary)
                            Text(r.prLabel).f(.callout).lineLimit(1)
                            if r.failed {
                                Text("실패").f(.caption2).foregroundStyle(.red)
                            } else if let e = r.event {
                                EventBadge(event: e)
                            }
                            Spacer()
                        }
                    }
                    .buttonStyle(.plain)
                }
            }
        }
    }

    // MARK: 카드

    private func card(tint: Color = .clear,
                      @ViewBuilder content: () -> some View) -> some View {
        content()
            .padding(14)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(tint == .clear ? AnyShapeStyle(.quaternary.opacity(0.3))
                                       : AnyShapeStyle(tint.opacity(0.10)),
                        in: RoundedRectangle(cornerRadius: 10))
    }
}

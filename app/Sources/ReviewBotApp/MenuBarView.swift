import SwiftUI

struct MenuBarView: View {
    @Environment(BotStore.self) private var store
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            Divider()
            if store.health == .running { liveBlock } else { nextTickBlock }
            if !store.drafts.isEmpty {
                Divider()
                draftBlock
            }
            Divider()
            recentBlock
            Divider()
            footer
        }
        .frame(width: 340)
    }

    // MARK: 머리

    private var header: some View {
        HStack(spacing: 8) {
            HealthDot(health: store.health, size: 9)
            Text("review-bot").f(.body, .semibold)
            Text(store.health.label).f(.caption).foregroundStyle(store.health.color)
            Spacer()
            Toggle(isOn: Binding(
                get: { store.autoPost },
                set: { on in Task { await store.setAutoPost(on) } }
            )) {
                Text(store.autoPost ? "자동 게시" : "검토 후")
                    .f(.caption2)
                    .foregroundStyle(store.autoPost ? Color.accentColor : .orange)
            }
            .toggleStyle(.switch)
            .controlSize(.mini)
            .disabled(store.busy != nil)
        }
        .padding(.horizontal, 12).padding(.vertical, 10)
    }

    // MARK: 다음 틱 / 진행 중

    private var nextTickBlock: some View {
        VStack(alignment: .leading, spacing: 4) {
            if let p = store.paused {
                Label("일시정지 — \(p.reason.isEmpty ? "사유 없음" : p.reason)",
                      systemImage: "pause.circle")
                    .f(.caption).foregroundStyle(.orange)
                Text("\(p.at) 부터").f(.caption2).foregroundStyle(.secondary)
            } else if let next = store.nextTick {
                TimelineView(.periodic(from: .now, by: 1)) { _ in
                    let left = max(0, Int(next.timeIntervalSinceNow))
                    HStack {
                        Text("다음 틱").f(.caption).foregroundStyle(.secondary)
                        Spacer()
                        Text(left > 0 ? String(format: "%d분 %02d초", left / 60, left % 60) : "곧")
                            .f(.caption, digits: true)
                    }
                }
            } else {
                Text("다음 틱 시각을 아직 모릅니다").f(.caption).foregroundStyle(.secondary)
            }
        }
        .padding(.horizontal, 12).padding(.vertical, 8)
    }

    private var liveBlock: some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(store.live) { s in
                VStack(alignment: .leading, spacing: 2) {
                    HStack(spacing: 6) {
                        ProgressView().controlSize(.mini)
                        Text(s.agent).f(.caption, .medium)
                        Text(s.pr).f(.caption2).foregroundStyle(.secondary)
                        Spacer()
                        Text("턴 " + String(s.turns)).f(.caption2, digits: true)
                            .foregroundStyle(.secondary)
                    }
                    if !s.lastDetail.isEmpty {
                        Text("\(s.lastTool) · \(s.lastDetail)")
                            .f(.caption2).foregroundStyle(.secondary).lineLimit(1)
                    }
                }
            }
            if let c = store.current {
                HStack(spacing: 6) {
                    Text(c.label).f(.caption2, .medium)
                    Text("\(c.author ?? "?") 작성").f(.caption2).foregroundStyle(.secondary)
                    if let by = c.requestedBy {
                        Text("· \(by) 요청").f(.caption2).foregroundStyle(.secondary)
                    }
                }
            }
            if store.live.isEmpty && store.current == nil {
                Text("리뷰 중 — 세션 준비하는 중").f(.caption).foregroundStyle(.secondary)
            }
        }
        .padding(.horizontal, 12).padding(.vertical, 8)
    }

    private var draftBlock: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(spacing: 5) {
                Image(systemName: "tray.full").f(.caption).foregroundStyle(.orange)
                Text("검토 대기 \(store.drafts.count)건").f(.caption, .medium)
            }
            ForEach(store.drafts.prefix(3)) { d in
                HStack(spacing: 6) {
                    Text(d.label).f(.caption)
                    EventBadge(event: d.event)
                    Spacer()
                    Text("인라인 \(d.inline ?? 0)").f(.caption2).foregroundStyle(.secondary)
                }
            }
            Text("대시보드에서 읽어 보고 게시할 수 있습니다")
                .f(.caption2).foregroundStyle(.tertiary)
        }
        .padding(.horizontal, 12).padding(.vertical, 8)
    }

    // MARK: 최근 리뷰

    private var recentBlock: some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(store.runs.prefix(3)) { r in
                HStack(spacing: 6) {
                    if r.failed {
                        Image(systemName: "xmark.circle.fill").foregroundStyle(.red).f(.caption)
                    } else if r.partial {
                        Image(systemName: "exclamationmark.triangle.fill")
                            .foregroundStyle(.orange).f(.caption)
                    } else {
                        Image(systemName: "checkmark.circle.fill").foregroundStyle(.green).f(.caption)
                    }
                    Text(r.prLabel).f(.caption, .medium)
                    if r.failed {
                        Text("실패").f(.caption2).foregroundStyle(.red)
                    } else {
                        EventBadge(event: r.event)
                        Text("인라인 \(r.inline ?? 0)").f(.caption2).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Text(shortTime(r.at)).f(.caption2, digits: true)
                        .foregroundStyle(.secondary)
                }
            }
            if store.runs.isEmpty {
                Text("아직 기록이 없습니다").f(.caption).foregroundStyle(.secondary)
            }
            Text("오늘 \(store.todaysRuns.count)건").f(.caption2).foregroundStyle(.secondary)
                .padding(.top, 2)
        }
        .padding(.horizontal, 12).padding(.vertical, 8)
    }

    // MARK: 발

    private var footer: some View {
        VStack(spacing: 6) {
            HStack(spacing: 6) {
                Button {
                    Task { await store.run([store.paused == nil ? "pause" : "resume"],
                                           label: store.paused == nil ? "일시정지" : "재개") }
                } label: {
                    Label(store.paused == nil ? "일시정지" : "재개",
                          systemImage: store.paused == nil ? "pause.fill" : "play.fill")
                        .frame(maxWidth: .infinity)
                }
                Button {
                    Task { await store.tickNow() }
                } label: {
                    Label("지금 틱", systemImage: "arrow.clockwise").frame(maxWidth: .infinity)
                }
                .disabled(store.paused != nil || store.health == .running)
            }
            .buttonStyle(.bordered)
            .controlSize(.small)

            Button {
                openWindow(id: "dashboard")
                NSApplication.shared.activate()
            } label: {
                Label("대시보드 열기", systemImage: "rectangle.on.rectangle")
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.small)

            HStack {
                if let busy = store.busy {
                    ProgressView().controlSize(.mini)
                    Text(busy).f(.caption2).foregroundStyle(.secondary)
                }
                Spacer()
                Button("종료") { NSApplication.shared.terminate(nil) }
                    .buttonStyle(.link).f(.caption2)
            }
        }
        .padding(.horizontal, 12).padding(.vertical, 10)
    }
}

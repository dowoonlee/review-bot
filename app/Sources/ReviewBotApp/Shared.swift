import SwiftUI

extension BotStore.Health {
    var label: String {
        switch self {
        case .running: "리뷰 중"
        case .paused:  "일시정지"
        case .idle:    "유휴"
        case .problem: "확인 필요"
        }
    }
    var color: Color {
        switch self {
        case .running: .blue
        case .paused:  .orange
        case .idle:    .green
        case .problem: .red
        }
    }
    var symbol: String {
        switch self {
        case .running: "arrow.triangle.2.circlepath"
        case .paused:  "pause.circle"
        case .idle:    "checkmark.circle"
        case .problem: "exclamationmark.triangle"
        }
    }
}

/// 상태 점 하나. 리뷰 중일 때만 숨을 쉰다.
struct HealthDot: View {
    let health: BotStore.Health
    var size: CGFloat = 8
    @State private var pulse = false

    var body: some View {
        Circle()
            .fill(health.color)
            .frame(width: size, height: size)
            .opacity(health == .running && pulse ? 0.35 : 1)
            .animation(health == .running
                       ? .easeInOut(duration: 0.9).repeatForever(autoreverses: true)
                       : .default, value: pulse)
            .onAppear { pulse = true }
    }
}

/// 판정 배지 — APPROVE / COMMENT / REQUEST_CHANGES.
struct EventBadge: View {
    let event: String?
    var body: some View {
        let e = event ?? "-"
        Text(text(e))
            .f(.caption2, .semibold)
            .padding(.horizontal, 6).padding(.vertical, 2)
            .background(color(e).opacity(0.15), in: Capsule())
            .foregroundStyle(color(e))
    }
    private func text(_ e: String) -> String {
        switch e {
        case "APPROVE": "승인"
        case "REQUEST_CHANGES": "변경 요청"
        case "COMMENT": "코멘트"
        case "PENDING": "검증 대기"
        case "DISCARDED": "정리됨"
        default: e
        }
    }
    private func color(_ e: String) -> Color {
        switch e {
        case "APPROVE": .green
        case "REQUEST_CHANGES": .red
        case "COMMENT": .blue
        default: .secondary
        }
    }
}

func md(_ s: String) -> AttributedString {
    (try? AttributedString(markdown: s, options: .init(
        interpretedSyntax: .full,
        failurePolicy: .returnPartiallyParsedIfPossible))) ?? AttributedString(s)
}

func shortTime(_ iso: String?) -> String {
    guard let iso, iso.count >= 19 else { return "-" }
    return String(iso.dropFirst(11))
}

func relativeTime(_ date: Date?) -> String {
    guard let date else { return "-" }
    let f = RelativeDateTimeFormatter()
    f.locale = Locale(identifier: "ko_KR")
    f.unitsStyle = .short
    return f.localizedString(for: date, relativeTo: Date())
}

// MARK: - 마크다운 블록 렌더

/// `AttributedString(markdown:)` 은 한 문자열 안의 인라인 서식만 살리고 제목·목록·
/// 줄바꿈 같은 블록 구조를 버린다. 리뷰 본문은 그 구조가 곧 읽는 순서라, 줄 단위로
/// 갈라서 블록마다 제 모양을 준다.
struct Markdown: View {
    let text: String

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            ForEach(Array(blocks.enumerated()), id: \.offset) { _, block in
                switch block {
                case .heading(let s, let level):
                    Text(inline(s))
                        .f(level <= 2 ? .headline : .subheadline, .semibold)
                        .padding(.top, 2)
                case .quote(let s):
                    HStack(alignment: .top, spacing: 8) {
                        Rectangle().fill(.tertiary).frame(width: 3)
                        Text(inline(s)).f(.callout).foregroundStyle(.secondary)
                    }
                    .fixedSize(horizontal: false, vertical: true)
                case .bullet(let s):
                    HStack(alignment: .top, spacing: 6) {
                        Text("·").f(.callout).foregroundStyle(.secondary)
                        Text(inline(s)).f(.callout)
                    }
                    .fixedSize(horizontal: false, vertical: true)
                case .rule:
                    Divider()
                case .paragraph(let s):
                    Text(inline(s)).f(.callout)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .textSelection(.enabled)
    }

    private enum Block {
        case heading(String, Int)
        case quote(String)
        case bullet(String)
        case rule
        case paragraph(String)
    }

    private var blocks: [Block] {
        var out: [Block] = []
        var buffer: [String] = []

        func flush() {
            let joined = buffer.joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines)
            if !joined.isEmpty { out.append(.paragraph(joined)) }
            buffer.removeAll()
        }

        for raw in text.components(separatedBy: .newlines) {
            let line = raw.trimmingCharacters(in: .whitespaces)
            if line.isEmpty { flush(); continue }
            if line.hasPrefix("<!--") { continue }          // 봇 마커는 사람에게 보일 것이 아니다
            if line.hasPrefix("---") || line.hasPrefix("***") { flush(); out.append(.rule); continue }
            if let hashes = line.range(of: "^#{1,6} ", options: .regularExpression) {
                flush()
                let level = line.distance(from: line.startIndex, to: hashes.upperBound) - 1
                out.append(.heading(String(line[hashes.upperBound...]), level))
                continue
            }
            if line.hasPrefix("> ") { flush(); out.append(.quote(String(line.dropFirst(2)))); continue }
            if line.hasPrefix("- ") || line.hasPrefix("* ") {
                flush(); out.append(.bullet(String(line.dropFirst(2)))); continue
            }
            buffer.append(line)
        }
        flush()
        return out
    }

    /// 블록 안의 인라인 서식만 해석한다 — 줄바꿈은 이미 블록이 지켰다.
    private func inline(_ s: String) -> AttributedString {
        (try? AttributedString(markdown: s, options: .init(
            interpretedSyntax: .inlineOnlyPreservingWhitespace,
            failurePolicy: .returnPartiallyParsedIfPossible))) ?? AttributedString(s)
    }
}

// MARK: - diff 문맥

/// 인라인 지적이 가리키는 줄을 앞뒤와 함께 git 처럼 보여 준다.
struct DiffHunkView: View {
    let lines: [HunkLine]
    let focus: Int?

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            ForEach(lines) { line in
                HStack(alignment: .top, spacing: 0) {
                    Text(line.old.map(String.init) ?? " ")
                        .frame(width: 38, alignment: .trailing)
                        .foregroundStyle(.tertiary)
                    Text(line.new.map(String.init) ?? " ")
                        .frame(width: 38, alignment: .trailing)
                        .foregroundStyle(isFocus(line) ? AnyShapeStyle(Color.primary)
                                                       : AnyShapeStyle(.tertiary))
                    Text(line.sign)
                        .frame(width: 16, alignment: .center)
                        .foregroundStyle(signColor(line.sign))
                    Text(line.text.isEmpty ? " " : line.text)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .f(.caption, mono: true)
                .padding(.vertical, 1)
                .padding(.horizontal, 4)
                .background(rowBackground(line))
                .overlay(alignment: .leading) {
                    if isFocus(line) {
                        Rectangle().fill(Color.accentColor).frame(width: 3)
                    }
                }
            }
        }
        .clipShape(RoundedRectangle(cornerRadius: 6))
        .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(.quaternary))
        .textSelection(.enabled)
    }

    private func isFocus(_ l: HunkLine) -> Bool { focus != nil && l.new == focus }

    private func signColor(_ sign: String) -> Color {
        switch sign {
        case "+": .green
        case "-": .red
        default: .secondary
        }
    }

    private func rowBackground(_ l: HunkLine) -> Color {
        if isFocus(l) { return Color.accentColor.opacity(0.16) }
        switch l.sign {
        case "+": return Color.green.opacity(0.10)
        case "-": return Color.red.opacity(0.10)
        default: return Color.clear
        }
    }
}

/// 제목 속 이슈 번호와 구분되도록 실제 PR 번호를 독립적으로 표시한다.
/// GitHub 의 Draft 표시. 글로 "초안" 이라 적는 대신 번호 옆에 회색 캡슐로 붙인다.
/// 왜 봇이 멈춰 있는지는 마우스를 올렸을 때(툴팁)만 보여 준다.
struct DraftBadge: View {
    var reason: String? = nil

    var body: some View {
        Label("Draft", systemImage: "pencil.line")
            .f(.caption2, .semibold)
            .labelStyle(.titleAndIcon)
            .foregroundStyle(.secondary)
            .padding(.horizontal, 7).padding(.vertical, 3)
            .overlay(Capsule().strokeBorder(.secondary.opacity(0.5), lineWidth: 1))
            .fixedSize()
            .help(reason ?? "초안(draft) PR")
            .accessibilityLabel(reason ?? "초안 PR")
    }
}

struct PRNumberBadge: View {
    let number: Int

    var body: some View {
        Text("#\(String(number))")
            .f(.body, .bold, mono: true)
            .foregroundStyle(Color.accentColor)
            .padding(.horizontal, 8).padding(.vertical, 4)
            .background(Color.accentColor.opacity(0.12), in: RoundedRectangle(cornerRadius: 5))
            .fixedSize()
            .accessibilityLabel("PR 번호 \(number)")
    }
}

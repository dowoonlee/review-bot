import Foundation

/// Bot-supplied task IDs take precedence over legacy prompt wording.
enum SessionLabel {
    static let pending = "세션 준비 중"

    static func parse(_ text: String) -> String? {
        let first = String(text.prefix(1500))
        let marker = "<!-- review-bot-task: "
        if first.hasPrefix(marker), let end = first.range(of: " -->") {
            return task(String(first[first.index(first.startIndex, offsetBy: marker.count)..<end.lowerBound]))
        }
        // Older transcripts have no task ID. Read only the leading instruction,
        // never a bold word quoted from PR code or an assistant response.
        let prefix = "당신은 **"
        if first.hasPrefix(prefix),
           let close = first.range(of: "**", range: first.index(first.startIndex, offsetBy: prefix.count)..<first.endIndex) {
            let start = first.index(first.startIndex, offsetBy: prefix.count)
            let name = String(first[start..<close.lowerBound])
            if validName(name) { return name }
        }
        if first.hasPrefix("당신은 PR 리뷰 지적 하나를 받아") { return "반증 검토" }
        if first.hasPrefix("당신은 PR 리뷰의 **최종 판정자**") { return "최종 판정" }
        if first.hasPrefix("당신은") && first.prefix(150).contains("편집") { return "문장 다듬기" }
        return nil
    }

    static func task(_ value: String) -> String? {
        if value.hasPrefix("batch-") {
            let parts = value.split(separator: "-", maxSplits: 2)
            if parts.count == 3, let number = Int(parts[1]), number > 0, validName(String(parts[2])) {
                return "\(parts[2]) · 묶음 \(number)"
            }
        }
        if value.hasPrefix("integration-") {
            let name = String(value.dropFirst("integration-".count))
            if validName(name) { return "\(name) · 통합 검토" }
        }
        if value.hasPrefix("verify-"), let number = Int(value.dropFirst("verify-".count)), number >= 0 {
            return "반증 검토 \(number + 1)"
        }
        if value == "verdict" { return "최종 판정" }
        if value == "polish" || value.hasPrefix("polish-") { return "문장 다듬기" }
        return validName(value) ? value : nil
    }

    private static func validName(_ value: String) -> Bool {
        !value.isEmpty && value.range(of: "^[A-Za-z0-9_-]+$", options: .regularExpression) != nil
    }
}

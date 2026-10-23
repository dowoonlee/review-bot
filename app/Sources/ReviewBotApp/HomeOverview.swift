import Foundation

struct HomeRequest: Identifiable {
    var id: String
    var repo: String
    var number: Int
    var title: String
    var action: String
    var reason: String
    var priority: Int
    var waiting: Bool
    var direction: String
    var selection: Selection
    var insight: RequestInsight?
    var stamp: HomeStamp
    var account: String?
    /// PR 의 head 브랜치. 기존 세션 연결에서 같은 브랜치를 체크아웃한 탭을 고르는 데 쓴다.
    var headRef: String?
    var currentAI: Bool { insight?.status == "ready" }
}

struct HomeStamp: Codable, Equatable {
    var state: String
    var checks: String
    var conflict: Bool
    var revision: String
    var analysis: String
}

struct HomeChange: Identifiable {
    var id: String { request.id }
    var request: HomeRequest
    var text: String
}

/// Facts determine blockers; only current Claude evidence contributes actionable prose.
struct HomeOverview {
    var requests: [HomeRequest]
    var completedIDs: Set<String> = []
    var activeTaskIDs: Set<String> = []
    var actions: [HomeRequest] { requests.filter { (!$0.waiting || activeTaskIDs.contains($0.id)) && !completedIDs.contains($0.id) }.sorted {
        if $0.priority != $1.priority { return $0.priority < $1.priority }
        let left = $0.insight?.selection
        let right = $1.insight?.selection
        let leftRank = left?.usable == true ? (left?.urgency ?? 1) : 1
        let rightRank = right?.usable == true ? (right?.urgency ?? 1) : 1
        if leftRank != rightRank { return leftRank > rightRank }
        return $0.id < $1.id
    } }
    var waiting: [HomeRequest] { requests.filter { $0.waiting && !activeTaskIDs.contains($0.id) && !completedIDs.contains($0.id) } }
    var ready: Int { requests.filter(\.currentAI).count }
    var failures: Int { requests.filter { ["error", "missing_key"].contains($0.insight?.status ?? "") }.count }
    var excluded: Int { requests.filter { $0.insight?.status == "disabled" }.count }
    var pending: Int { requests.filter { ["pending", "stale"].contains($0.insight?.status ?? "pending") }.count }
    var incomplete: Int { requests.filter { $0.currentAI && $0.insight?.incomplete == true }.count }
    var lastAnalysis: String? { requests.compactMap { $0.insight?.analyzedAt }.max() }
    var stamps: [String: HomeStamp] { Dictionary(uniqueKeysWithValues: requests.map { ($0.id, $0.stamp) }) }

    init(_ status: StatusSnapshot?, tasks: HomeTaskFile = .empty) {
        completedIDs = Set(tasks.completed.map(\.requestID))
        activeTaskIDs = Set(tasks.current.values.compactMap { tasks.tasks[$0] }.filter { ["running", "stopped", "reopened"].contains($0.status) }.map(\.requestID))
        var rows: [HomeRequest] = []
        for pr in status?.outgoing ?? [] {
            let ai = pr.insight?.status == "ready" ? pr.insight : nil
            let items = ai?.items ?? []
            // 보호 규칙이 없는 저장소는 reviewDecision 이 비므로 리뷰어 전원 승인·대기 없음을 승인으로 본다.
            let approved = pr.reviewDecision == "APPROVED"
                || ((pr.reviewDecision ?? "").isEmpty && !pr.reviewers.isEmpty && pr.reviewers.allSatisfy { $0.state == "APPROVED" })
            var action = "리뷰어 답변 대기", reason = "리뷰 답변이 오면 다시 확인하세요."
            var priority = 9, waiting = true
            if pr.isDraft {
                action = "초안 작성 중"; reason = "리뷰 요청 전에 PR을 준비하고 있습니다."
            } else if pr.mergeable == "CONFLICTING" {
                action = "충돌 해결"; reason = "브랜치 충돌이 감지됐습니다."; priority = 0; waiting = false
            } else if ["FAILURE", "ERROR"].contains(pr.checksState ?? "") {
                action = "CI 실패 확인"; reason = "실패한 검증을 확인하세요."; priority = 0; waiting = false
            } else if pr.reviewDecision == "CHANGES_REQUESTED" || items.contains(where: { $0.kind == "change" }) {
                action = "수정 요청 검토"; reason = items.first(where: { $0.kind == "change" })?.text ?? "리뷰어의 변경 요청을 확인하세요."
                priority = 1; waiting = false
            } else if let question = items.first(where: { $0.kind == "question" }) {
                action = "질문 답변"; reason = question.text; priority = 2; waiting = false
            } else if let item = items.first {
                action = "후속 확인"; reason = item.text; priority = 4; waiting = false
            } else if ["PENDING", "EXPECTED"].contains(pr.checksState ?? "") {
                action = "CI 완료 대기"; reason = "실행 중인 검증 결과를 기다리고 있습니다."
            } else if approved {
                action = "최종 머지 조건 확인"; reason = "승인은 충족됐습니다. 필수 검사와 저장소의 머지 조건을 확인하세요."
                priority = 5; waiting = false
            } else if pr.reviewers.contains(where: { $0.state == "COMMENTED" }) {
                action = "리뷰 코멘트 확인"; reason = "리뷰어가 남긴 코멘트가 있습니다."; priority = 4; waiting = false
            }
            rows.append(HomeRequest(id: "outgoing:" + pr.id, repo: pr.repo, number: pr.number,
                title: pr.title, action: action, reason: reason, priority: priority, waiting: waiting,
                direction: "보낸 요청", selection: .sent(pr.id), insight: pr.insight,
                stamp: HomeStamp(state: pr.reviewDecision ?? "", checks: pr.checksState ?? "",
                    conflict: pr.mergeable == "CONFLICTING", revision: pr.updatedAt + (pr.responses ?? []).map(\.id).sorted().joined(separator: ","),
                    analysis: ai.map { ($0.summary ?? "") + items.map(\.text).joined(separator: "\n") } ?? ""), account: pr.account,
                headRef: pr.headRefName))
        }
        for pr in status?.pending ?? [] {
            let ai = pr.insight?.status == "ready" ? pr.insight : nil
            let waiting = pr.status == "pending" || pr.status == "discarded" || pr.isDraft == true
            let action = pr.isDraft == true ? "초안 PR — 자동 리뷰 보류" : pr.status == "pending" ? "실행 검증 대기" : pr.status == "discarded" ? "로컬 자료 정리됨" : pr.status == "draft" ? "봇 리뷰 초안 확인" : pr.status == "posted" ? "재요청·후속 대화 확인" : "요청받은 리뷰 진행"
            rows.append(HomeRequest(id: "incoming:" + (pr.account ?? "global") + ":" + pr.id,
                repo: pr.repo, number: pr.number, title: pr.title, action: action,
                reason: pr.holdReason ?? ai?.items?.first?.text ?? (pr.status == "draft" ? "게시 전 봇의 리뷰 초안을 확인하세요." : "받은 리뷰 요청의 변경 내용과 대화를 확인하세요."),
                priority: 3, waiting: waiting, direction: "받은 요청", selection: .pending(pr.id), insight: pr.insight,
                stamp: HomeStamp(state: pr.status, checks: "", conflict: false, revision: pr.updatedAt,
                    analysis: ai.map { ($0.summary ?? "") + ($0.items ?? []).map(\.text).joined(separator: "\n") } ?? ""), account: pr.account,
                headRef: pr.headRefName))
        }
        if status?.selectionSettings?.enabled != true {
            for i in rows.indices { rows[i].insight?.selection = nil }
        }
        if status?.selectionSettings?.enabled == true {
            for i in rows.indices {
                guard let decision = rows[i].insight?.selection, decision.usable,
                      rows[i].priority > 0, rows[i].action != "초안 작성 중", rows[i].action != "초안 PR — 자동 리뷰 보류",
                      rows[i].action != "실행 검증 대기", rows[i].action != "로컬 자료 정리됨",
                      rows[i].action != "봇 리뷰 초안 확인" else { continue }
                if decision.actor == "self" {
                    if decision.action == "reply" && rows[i].priority != 1 {
                        rows[i].action = "질문 답변"; rows[i].priority = 2; rows[i].waiting = false
                        rows[i].reason = "Jev가 내 답변이 필요한 대화로 분류했습니다. 리뷰 원문을 확인해 주세요."
                    } else if decision.action == "fix" {
                        rows[i].action = "수정 요청 검토"; rows[i].priority = 1; rows[i].waiting = false
                        rows[i].reason = "Jev가 내가 검토할 수정 요구로 분류했습니다. 원문과 코드를 함께 확인해 주세요."
                    } else if decision.action == "review" && rows[i].waiting {
                        rows[i].action = "후속 리뷰 확인"; rows[i].priority = 4; rows[i].waiting = false
                        rows[i].reason = "Jev가 내 후속 리뷰가 필요한 것으로 분류했습니다."
                    }
                } else if decision.actor == "other", decision.action == "wait", rows[i].priority != 1, rows[i].action != "최종 머지 조건 확인" {
                    rows[i].action = "상대 응답 대기"; rows[i].waiting = true
                    rows[i].reason = "Jev가 상대의 후속 답변을 기다릴 차례로 분류했습니다."
                }
                rows[i].stamp.analysis += "\nJev: " + (decision.actor ?? "") + ":" + (decision.action ?? "")
            }
        }
        // Account + direction keys prevent duplicates from inflating counts.
        requests = Array(Dictionary(rows.map { ($0.id, $0) }, uniquingKeysWith: { a, _ in a }).values).sorted { $0.id < $1.id }
    }

    func changes(since baseline: [String: HomeStamp]?) -> [HomeChange] {
        guard let baseline else { return [] }
        return requests.compactMap { row in
            guard let old = baseline[row.id] else { return HomeChange(request: row, text: "새 \(row.direction)") }
            let now = row.stamp
            var changes: [String] = []
            if old.state != now.state { changes.append("상태 변경: \(row.action)") }
            if old.checks != now.checks { changes.append(["FAILURE", "ERROR"].contains(now.checks) ? "CI 실패 발생" : now.checks == "SUCCESS" ? "CI 통과" : "CI 상태 변경") }
            if old.conflict != now.conflict { changes.append(now.conflict ? "충돌 발생" : "충돌 해소") }
            if !now.analysis.isEmpty && old.analysis != now.analysis { changes.append("분석·대응 항목 갱신") }
            if changes.isEmpty && old.revision != now.revision { changes.append("PR에 새 변경·대화") }
            return changes.isEmpty ? nil : HomeChange(request: row, text: changes.joined(separator: " · "))
        }
    }
}

import Foundation

// MARK: - 경로

/// 봇이 남기는 파일들의 위치. 앱은 이 파일들만 읽고, 토큰은 절대 다루지 않는다.
enum BotPaths {
    /// 봇 루트. 환경변수 > 사용자 설정 > 기본값 순으로 고른다.
    static var root: URL {
        if let env = ProcessInfo.processInfo.environment["REVIEW_BOT_ROOT"], !env.isEmpty {
            return URL(fileURLWithPath: (env as NSString).expandingTildeInPath)
        }
        if let saved = UserDefaults.standard.string(forKey: "botRoot"), !saved.isEmpty {
            return URL(fileURLWithPath: (saved as NSString).expandingTildeInPath)
        }
        return URL(fileURLWithPath: NSHomeDirectory()).appending(path: ".dev/review-bot")
    }

    static var executable: URL { root.appending(path: "review-bot") }
    static var config: URL { root.appending(path: "config.json") }
    static var state: URL { root.appending(path: "state") }
    static var current: URL { state.appending(path: "current.json") }
    static var schedule: URL { state.appending(path: "schedule.json") }
    static var runsLog: URL { state.appending(path: "runs.jsonl") }
    static var botLog: URL { state.appending(path: "bot.log") }
    /// 날짜별 틱 로그가 사는 곳. 사람이 읽는 것은 이쪽이다.
    static var logsDir: URL { state.appending(path: "logs") }
    static func dayLog(_ day: String) -> URL {
        logsDir.appending(path: "\(day).log")
    }
    static var today: String {
        let f = DateFormatter(); f.dateFormat = "yyyy-MM-dd"
        return f.string(from: Date())
    }
    static var reviewed: URL { state.appending(path: "reviewed.json") }
    static var paused: URL { state.appending(path: "PAUSED") }
    static var lock: URL { state.appending(path: "tick.lock") }
    static var drafts: URL { state.appending(path: "drafts") }
    static var worktrees: URL { state.appending(path: "wt") }

    /// claude 가 cwd 를 프로젝트 디렉터리 이름으로 바꾸는 규칙 (lib/janitor.py 와 같아야 한다).
    static func sessionDir(forWorktree path: String) -> URL {
        var name = path
        for ch in ["/", ".", "_"] { name = name.replacingOccurrences(of: ch, with: "-") }
        return URL(fileURLWithPath: NSHomeDirectory())
            .appending(path: ".claude/projects").appending(path: name)
    }
}

// MARK: - 봇이 남긴 기록

/// runs.jsonl 한 줄. 성공한 리뷰와 실패한 리뷰가 같은 파일에 섞여 있어 대부분 옵셔널이다.
struct DecisionReason: Decodable, Sendable, Identifiable {
    var code: String
    var message: String
    var source: String?
    var id: String { code }
    var label: String {
        switch code {
        case "DEFECT": "수정 필요"
        case "HUMAN_REVIEW": "사람 확인 필요"
        case "ENVIRONMENT_UNAVAILABLE": "실행 환경 대기"
        case "CI_PENDING": "CI 대기"
        case "CI_FAILED": "CI 실패"
        case "REVIEW_INCOMPLETE": "코드 검토 미완료"
        case "VALIDATION_INCOMPLETE": "실행 검증 미완료"
        case "JUDGE_FAILED": "판정 생성 실패"
        case "PUBLISH_FAILED": "게시 실패"
        default: "판정 조건"
        }
    }
}

struct DecisionRecord: Decodable, Sendable {
    var judgeEvent: String?
    var finalEvent: String?
    var judgeReason: String?
    var summary: String?
    var reasons: [DecisionReason]?
}

struct RunRecord: Decodable, Identifiable, Sendable {
    var repo: String?
    var number: Int?
    var title: String?
    var account: String?
    var sha: String?
    var costUSD: Double?
    var inline: Int?
    var demoted: Int?
    var dropped: Int?
    var event: String?
    var reason: String?
    var decision: DecisionRecord?
    var summary: String?
    var draft: String?
    var posted: Bool?
    var at: String?
    var error: String?
    var agentErrors: [String]?
    var reviewDecision: String?
    var lostAnchors: Int?
    var degraded: Bool?
    var postError: String?
    /// 크기 상한에 걸려 리뷰하지 않은 경우의 사유. 리뷰 기록과 같은 파일에 섞인다.
    var skipped: String?
    var files: Int?
    var churn: Int?

    var id: String { "\(repo ?? "?")#\(number ?? 0)@\(at ?? "?")" }

    enum CodingKeys: String, CodingKey {
        case repo, number, title, account, sha, inline, demoted, dropped
        case event, reason, decision, summary, draft, posted, at, error, degraded
        case reviewDecision, lostAnchors, skipped, files, churn
        case costUSD = "cost_usd"
        case agentErrors = "agent_errors"
        case postError = "post_error"
    }

    var prLabel: String { "\(repo?.split(separator: "/").last.map(String.init) ?? "?")#\(number ?? 0)" }
    var findingCount: Int { (inline ?? 0) + (demoted ?? 0) }
    var failed: Bool { error != nil }
    var wasSkipped: Bool { skipped != nil }
    var partial: Bool { !(agentErrors ?? []).isEmpty }
    var date: Date? { RunRecord.stamp.date(from: at ?? "") }

    static let stamp: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
        f.timeZone = .current
        return f
    }()
}

/// reviewed.json 의 값 — 같은 커밋을 두 번 리뷰하지 않기 위한 지문.
struct ReviewedRecord: Decodable, Sendable {
    var at: String?
    var sha: String?
    var event: String?
    var inline: Int?
    var demoted: Int?
    var posted: Bool?
    var decision: DecisionRecord?
    var costUSD: Double?

    enum CodingKeys: String, CodingKey {
        case at, sha, event, inline, demoted, posted, decision
        case costUSD = "cost_usd"
    }
}

// MARK: - review-bot status --json

struct StatusSnapshot: Decodable, Sendable {
    var at: String
    var accounts: [AccountInfo]
    var repos: [String]
    var repoDetails: [RepoDetail]?
    var dryRun: Bool
    var maxPerTick: Int
    var paused: PausedInfo?
    var running: RunningInfo?
    var pending: [PendingPR]
    var outgoing: [SentPR]?
    var insightSettings: InsightSettings?
    var selectionSettings: SelectionSettings?
    var outgoingErrors: [String]?
    var issues: [AssignedIssue]?
    var issueErrors: [String]?
    var outgoingFailedAccounts: [String]?
    var config: BotConfig?
    var reviewed: [String: ReviewedRecord]
    var errors: [String]
}

struct AccountInfo: Decodable, Sendable, Identifiable {
    var account: String?
    var repos: [String]
    var tokenOwner: String?
    var ok: Bool
    var error: String?
    var id: String { account ?? "global" }
}

struct PausedInfo: Decodable, Sendable {
    var at: String
    var reason: String
}

struct RunningInfo: Decodable, Sendable {
    var pid: Int
    var since: String
    var alive: Bool
}

/// 초안에 손대는 버튼(게시·게시 방식 바꾸기)을 언제 막는가.
///
/// 봇의 `publish` 와 `drafts event` 는 틱 전체가 아니라 그 PR 만 잠근다. 그래서 봇이
/// 돌고 있어도 **다른** PR 을 리뷰하는 중이면 이 초안은 그대로 올릴 수 있다. 막아야
/// 하는 것은 같은 PR 을 리뷰하는 중일 때 — 그때는 봇이 초안을 다시 쓰고 있어 CLI 가
/// 거절한다 — 와 앱이 이미 다른 명령을 돌리고 있을 때뿐이다.
enum DraftGate {
    static func blocked(busy: Bool, running: Bool, current: CurrentRun?,
                        repo: String, number: Int) -> Bool {
        if busy { return true }
        guard running, let c = current else { return false }
        return c.repo == repo && c.number == number
    }
}

struct PendingPR: Decodable, Sendable, Identifiable {
    var account: String?
    var insight: RequestInsight?
    var repo: String
    var number: Int
    var title: String
    var url: String
    var author: String
    var updatedAt: String
    var watched: Bool
    var status: String          // new | draft | posted | unwatched
    var event: String?
    var lastSha: String?
    var requestedBy: String?    // 나에게 리뷰를 요청한 사람
    var prAuthor: String?
    var isDraft: Bool?
    var holdReason: String?    // 틱이 리뷰하지 않고 넘기는 이유 (draft · 본인 PR)
    var changedFiles: Int?
    var issues: [LinkedIssue]?
    var headRefName: String?

    var id: String { "\(repo)#\(number)" }
    var label: String { "\(repo.split(separator: "/").last.map(String.init) ?? repo)#\(number)" }
}

struct SentResponse: Decodable, Sendable {
    var id: String
}

struct SentPR: Decodable, Sendable, Identifiable {
    var responses: [SentResponse]?
    var insight: RequestInsight?
    var repo: String
    var number: Int
    var title: String
    var url: String
    var author: String
    var account: String
    var updatedAt: String
    var isDraft: Bool
    var reviewDecision: String?
    var mergeable: String?
    var checksState: String?
    var requestedAt: String?
    var reviewers: [PRReviewer]
    var headRefName: String?

    var id: String { "\(account):\(repo)#\(number)" }
    var label: String { "\(repo)#\(number)" }
    var reviewLabel: String {
        switch reviewDecision {
        case "CHANGES_REQUESTED": "수정 필요"
        case "APPROVED": "승인 충족"
        case "REVIEW_REQUIRED": "리뷰 대기"
        default: reviewers.contains { $0.state == "PENDING" } ? "리뷰 대기" : "리뷰 확인"
        }
    }
    // 대응할 것(수정 필요·리뷰 확인·승인 충족)을 위로, 아직 리뷰가 안 달린 것은 맨 뒤로 보낸다.
    var priority: Int {
        switch reviewLabel {
        case "수정 필요": 0
        case "리뷰 확인": 1
        case "승인 충족": 2
        default: 3
        }
    }
    var checksLabel: String {
        switch checksState {
        case "SUCCESS": "CI 통과"
        case "FAILURE", "ERROR": "CI 실패"
        case "PENDING", "EXPECTED": "CI 대기"
        default: "CI 정보 없음"
        }
    }
}

struct PRReviewer: Decodable, Sendable, Identifiable {
    var name: String
    var state: String
    var id: String { name }
    var label: String {
        switch state {
        case "PENDING": "대기"
        case "APPROVED": "승인"
        case "CHANGES_REQUESTED": "변경 요청"
        case "COMMENTED": "코멘트"
        case "DISMISSED": "리뷰 무효화"
        default: "확인 필요"
        }
    }
    /// 리뷰어별 묶음 안의 정렬. 답을 남긴 것(변경 요청 → 코멘트 등 → 승인)이 위, 대기가 맨 아래.
    var priority: Int {
        switch state {
        case "CHANGES_REQUESTED": 0
        case "APPROVED": 2
        case "PENDING": 3
        default: 1
        }
    }
}

// MARK: - 봇 로그

struct LogLine: Identifiable, Sendable {
    let id: Int
    let time: String
    let text: String
    var indented: Bool
    var isError: Bool
}

// MARK: - 살아 있는 리뷰어 세션

/// 워크트리에서 도는 `claude -p` 세션 하나. transcript 를 증분으로 읽어 만든다.
struct LiveSession: Identifiable, Sendable {
    var id: String              // 세션 파일 경로
    var agent: String           // code-reviewer / security-reviewer
    var pr: String              // owner/repo#123
    var turns: Int
    var tools: [String: Int]
    var lastTool: String
    var lastDetail: String
    var updatedAt: Date
    var deniedTools: Int

    var toolSummary: String {
        tools.sorted { $0.key < $1.key }.map { "\($0.key) \($0.value)" }.joined(separator: " · ")
    }
    var idleSeconds: Int { max(0, Int(Date().timeIntervalSince(updatedAt))) }
}

// MARK: - 게시를 기다리는 초안

/// state/drafts/<repo>-<n>.json — 리뷰를 다시 돌리지 않고 그대로 올릴 수 있는 사본.
struct DraftPayload: Decodable, Sendable, Identifiable {
    var repo: String
    var number: Int
    var title: String?
    var sha: String?
    var account: String?
    var event: String?
    var reason: String?
    var body: String?
    var comments: [DraftComment]?
    var inline: Int?
    var demoted: Int?
    var dropped: [DroppedFinding]?
    var costUSD: Double?
    var at: String?
    var overridden: DraftOverride?   // 사용자가 판정과 다른 이벤트로 바꿨을 때 원래 판정

    var id: String { "\(repo)#\(number)" }
    var label: String { "\(repo.split(separator: "/").last.map(String.init) ?? repo)#\(number)" }

    /// 게시될 이벤트. 봇과 같은 규칙으로, 비어 있으면 코멘트다.
    var effectiveEvent: String { event ?? "COMMENT" }
    /// 사람이 읽고 다른 이벤트로 올리기로 할 수 있다 — 지금 이벤트를 뺀 나머지.
    /// `review-bot drafts event` 가 받는 순서와 같다.
    static let events = ["APPROVE", "COMMENT", "REQUEST_CHANGES"]
    var switchableEvents: [String] { Self.events.filter { $0 != effectiveEvent } }
    /// 사용자가 바꾼 초안이면 판정으로 되돌릴 이벤트.
    var revertEvent: String? {
        guard let from = overridden?.from, from != effectiveEvent else { return nil }
        return from
    }

    var notificationID: String { "\(id)|\(sha ?? "")|\(at ?? "")" }

    static func needingNotification(_ drafts: [DraftPayload], seen: Set<String>) -> [DraftPayload] {
        drafts.filter { !seen.contains($0.notificationID) }
    }

    enum CodingKeys: String, CodingKey {
        case repo, number, title, sha, account, event, reason, body
        case comments, inline, demoted, dropped, at
        case costUSD = "cost_usd"
        case overridden = "override"
    }
}

/// 사용자가 초안의 이벤트를 바꾸기 전 판정. `review-bot drafts event` 가 남긴다.
struct DraftOverride: Decodable, Sendable {
    var from: String?
    var at: String?
}

/// 리뷰 이벤트의 한국어 이름 — 배지·메뉴·명령 라벨이 같은 말을 쓴다.
func eventLabel(_ event: String) -> String {
    switch event {
    case "APPROVE": "승인"
    case "REQUEST_CHANGES": "변경 요청"
    case "COMMENT": "코멘트"
    default: event
    }
}

struct DraftComment: Decodable, Sendable, Identifiable {
    var path: String
    var line: Int?
    var body: String
    var hunk: [HunkLine]?       // 지적이 가리키는 줄의 앞뒤 diff
    var id: String { "\(path):\(line ?? 0)" }
}

/// diff 한 줄. sign 은 git 그대로 " " / "+" / "-".
struct HunkLine: Decodable, Sendable, Identifiable {
    var old: Int?
    var new: Int?
    var sign: String
    var text: String
    var id: String { "\(old ?? -1)-\(new ?? -1)-\(sign)" }
}

/// PR 이 가리키는 이슈.
struct LinkedIssue: Decodable, Sendable, Identifiable {
    var number: Int
    var title: String
    var author: String
    var state: String
    var url: String
    var id: Int { number }
}

/// 감시 저장소 하나. UI 가 켜고 끄고 지울 수 있도록 상세까지 받는다.
struct RepoDetail: Decodable, Sendable, Identifiable {
    var slug: String
    var enabled: Bool
    var account: String
    var localPath: String
    var cloned: Bool
    var id: String { slug }
}

/// review-bot 설정 스냅샷 — 무슨 리뷰어가 무슨 도구로 도는지.
struct BotConfig: Decodable, Sendable {
    var account: String?
    var model: String?
    var judgeModel: String?
    var parallel: Bool?
    var maxTurns: Int?
    var timeoutSec: Int?
    var maxChangedFiles: Int?
    var maxDiffLines: Int?
    var batchMaxFiles: Int?
    var batchMaxChars: Int?
    var workers: Int?
    var integrationMode: String?
    var minSeverity: String?
    var maxFindingsPerPR: Int?
    var allowedEvents: [String]?
    var autonomous: Bool?
    var maxPerTick: Int?
    var intervalSec: Int?
    var botTools: [String]?
    var agents: [AgentInfo]?
}

struct AgentInfo: Decodable, Sendable, Identifiable {
    var name: String
    var exists: Bool
    var description: String
    /// 정의 파일 frontmatter 의 model. 봇은 이것을 쓰지 않는다 — 참고용.
    var model: String
    /// 이 리뷰어가 실제로 도는 모델. config 가 정한다.
    var runsAs: String?
    var tools: [String]
    var role: String
    var path: String?
    var error: String?
    var id: String { name }
}

struct DroppedFinding: Decodable, Sendable, Identifiable {
    var severity: String?
    var title: String?
    var id: String { "\(severity ?? "")-\(title ?? "")" }
}


/// 지금 리뷰 중인 PR. 봇이 리뷰를 시작할 때 state/current.json 에 남긴다.
struct ReviewCoverage: Decodable, Sendable {
    var batches: Int
    var planned: Int
    var completed: Int
    var integrationPlanned: Int
    var integrationCompleted: Int
}

struct CurrentRun: Decodable, Sendable {
    var repo: String
    var number: Int
    var title: String?
    var author: String?
    var requestedBy: String?
    var coverage: ReviewCoverage?
    var sha: String?
    var account: String?
    var files: Int?
    var churn: Int?
    var agents: [String]?
    var startedAt: String?
    var url: String?

    var label: String { "\(repo.split(separator: "/").last.map(String.init) ?? repo)#\(number)" }
    var started: Date? { RunRecord.stamp.date(from: startedAt ?? "") }
}

/// 같은 PR 의 리뷰 실행들을 한 묶음으로. 목록이 PR 단위로 정리되어야 왜 여러 번
/// 돌았는지가 보인다.
struct RunGroup: Identifiable, Sendable {
    var id: String
    var repo: String
    var number: Int
    var runs: [RunRecord]
    var label: String { "\(repo.split(separator: "/").last.map(String.init) ?? repo)#\(number)" }
    var latestAt: String { runs.first?.at ?? "" }
    var posted: Bool { runs.contains { $0.posted == true } }
}


/// 다음 틱을 셀 기준점. launchd 는 다음 발사 시각을 알려주지 않으므로, 타이머가
/// 다시 시작된 순간(launchd 가 띄운 틱 / job 재적재)을 봇이 남긴 것을 읽는다.
struct Schedule: Decodable, Sendable {
    var at: String
    var reason: String
    var intervalSec: Int?
    var anchor: Date? { RunRecord.stamp.date(from: at) }
}

// MARK: - 나에게 할당된 이슈

/// Jev 가 이슈 하나에 내린 판단. PR 대화용(JevDecision)과 답의 모양이 다르다.
struct IssueDecision: Decodable, Sendable {
    var status: String          // pending | ready | uncertain | stale | error
    var state: String?          // actionable | blocked | needsInfo | resolved | unknown
    var urgency: Double?
    var confidence: Double?
    var analyzedAt: String?
    var error: String?
    var incomplete: Bool?
    var usable: Bool { status == "ready" && (confidence ?? 0) >= 0.8 }
    var stateLabel: String? {
        guard usable else { return nil }
        return switch state {
        case "actionable": "바로 착수 가능"
        case "blocked": "다른 사람·의존성 대기"
        case "needsInfo": "보고자 확인 필요"
        case "resolved": "완료로 보임 · 닫기 검토"
        default: nil
        }
    }
    var statusLabel: String {
        switch status {
        case "ready": usable ? "Jev 분류" : "Jev 확신 부족"
        case "uncertain": "Jev 확신 부족"
        case "stale": "Jev · 새 변경 감지"
        case "error": "Jev 오류"
        default: "Jev 분류 대기"
        }
    }
}

/// 봇이 정한 정렬 점수. 출처가 Jev 면 분류 점수, 아니면 GitHub Priority 필드다.
struct IssueRank: Decodable, Sendable {
    var score: Double
    var source: String          // jev | priority | none
}

struct AssignedIssue: Decodable, Sendable, Identifiable {
    var repo: String
    var number: Int
    var title: String
    var url: String
    var author: String
    var account: String
    var createdAt: String
    var updatedAt: String
    var labels: [String]
    var milestone: String?
    var milestoneDue: String?
    var comments: Int?
    var type: String?
    var priority: String?       // Urgent | High | Medium | Low — org 이슈 필드
    var targetDate: String?
    var watched: Bool?
    var jev: IssueDecision?
    var rank: IssueRank?

    var id: String { "\(account):\(repo)#\(number)" }
    var label: String { "\(repo.split(separator: "/").last.map(String.init) ?? repo)#\(number)" }
    var score: Double { rank?.score ?? 1.0 }
    /// 점수를 사람 말로. 2 에 가까우면 긴급, 1 근처면 보통, 그 아래는 낮음.
    var urgencyLabel: String {
        if score >= 1.75 { return "긴급" }
        if score >= 1.25 { return "높음" }
        if score >= 0.75 { return "보통" }
        return "낮음"
    }
    var priorityLabel: String {
        switch priority {
        case "Urgent": "Urgent"
        case "High": "High"
        case "Medium": "Medium"
        case "Low": "Low"
        default: "우선순위 미지정"
        }
    }
    /// 날짜가 없는 것은 맨 뒤로 보내기 위한 비교 키. ISO 날짜라 문자열 비교로 충분하다.
    var dueKey: String { targetDate ?? milestoneDue ?? "9999" }

    /// 긴급한 것부터. 같은 점수면 마감이 가까운 것, 그 다음 최근에 움직인 것.
    static func urgentFirst(_ a: AssignedIssue, _ b: AssignedIssue) -> Bool {
        if a.score != b.score { return a.score > b.score }
        if a.dueKey != b.dueKey { return a.dueKey < b.dueKey }
        if a.updatedAt != b.updatedAt { return a.updatedAt > b.updatedAt }
        return a.id < b.id
    }
}

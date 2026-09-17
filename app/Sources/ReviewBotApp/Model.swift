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

    var id: String { "\(repo ?? "?")#\(number ?? 0)@\(at ?? "?")" }

    enum CodingKeys: String, CodingKey {
        case repo, number, title, account, sha, inline, demoted, dropped
        case event, reason, summary, draft, posted, at, error, degraded
        case reviewDecision, lostAnchors
        case costUSD = "cost_usd"
        case agentErrors = "agent_errors"
        case postError = "post_error"
    }

    var prLabel: String { "\(repo?.split(separator: "/").last.map(String.init) ?? "?")#\(number ?? 0)" }
    var findingCount: Int { (inline ?? 0) + (demoted ?? 0) }
    var failed: Bool { error != nil }
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
    var costUSD: Double?

    enum CodingKeys: String, CodingKey {
        case at, sha, event, inline, demoted, posted
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
    var outgoingErrors: [String]?
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

struct PendingPR: Decodable, Sendable, Identifiable {
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
    var changedFiles: Int?
    var issues: [LinkedIssue]?

    var id: String { "\(repo)#\(number)" }
    var label: String { "\(repo.split(separator: "/").last.map(String.init) ?? repo)#\(number)" }
}

struct SentPR: Decodable, Sendable, Identifiable {
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

    var id: String { "\(repo)#\(number)" }
    var label: String { "\(repo.split(separator: "/").last.map(String.init) ?? repo)#\(number)" }

    enum CodingKeys: String, CodingKey {
        case repo, number, title, sha, account, event, reason, body
        case comments, inline, demoted, dropped, at
        case costUSD = "cost_usd"
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
struct CurrentRun: Decodable, Sendable {
    var repo: String
    var number: Int
    var title: String?
    var author: String?
    var requestedBy: String?
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

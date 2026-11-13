import Foundation
import Observation

/// 봇의 상태를 한곳에 모은다.
///
/// 로컬 파일(로그·이력·lock·PAUSED)은 2초마다 증분으로 읽고, gh 조회가 필요한
/// `status --json` 은 네트워크를 타므로 60초마다 또는 사용자가 새로고침할 때만 부른다.
@MainActor
@Observable
final class BotStore {
    let appVersion = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "개발 빌드"
    var updateSnapshot: UpdateSnapshot?
    var updateError: String?
    var checkingUpdates = false
    var lastUpdateCheckAttempt: Date?
    private var updateCheckTask: Task<Void, Never>?
    var runs: [RunRecord] = []
    var logLines: [LogLine] = []
    /// 지금 보고 있는 날짜. 기본은 오늘 — 지난 로그는 사용자가 고를 때만 읽는다.
    var logDay: String = BotPaths.today
    /// 로그가 있는 날짜, 최근 것부터.
    var logDays: [String] = []
    var reviewed: [String: ReviewedRecord] = [:]
    var status: StatusSnapshot?
    var live: [LiveSession] = []
    var drafts: [DraftPayload] = []
    var dryRunLocal: Bool?
    var current: CurrentRun?
    /// 봇이 바쁠 때 요청해 대기 중인 리뷰. 하던 PR 이 끝나면 앞에서부터 처리된다.
    var queue: [QueuedReview] = []
    var schedule: Schedule?

    var paused: PausedInfo?
    var running: RunningInfo?
    var nextTick: Date?
    var tickInterval: TimeInterval = 900
    var lastCommandOutput: String = ""
    var busy: String?               // 실행 중인 명령 이름
    var statusError: String?
    var statusLoadedAt: Date?
    var isRefreshingStatus = false
    var homeBaseline: [String: HomeStamp]? = UserDefaults.standard.data(forKey: "homeOverviewBaseline")
        .flatMap { try? JSONDecoder().decode([String: HomeStamp].self, from: $0) }
    var homeAcknowledgedAt = UserDefaults.standard.object(forKey: "homeOverviewAcknowledgedAt") as? Date
    var homeTasks = HomeTaskFile.empty
    private var homeTaskData: Data?
    var homeTaskError: String?
    /// 할 일 id → 연결된 iTerm 세션이 살아 있는지. 새로고침 때마다 다시 본다. 없으면 확인 못 한 것.
    var sessionLiveness: [String: Bool] = [:]
    var sessionCheckError: String?
    var homeOverview: HomeOverview { HomeOverview(status, tasks: homeTasks) }
    var homeChanges: [HomeChange] { homeOverview.changes(since: homeBaseline) }
    var homeSnapshotComplete: Bool {
        status != nil && statusError == nil && (status?.errors.isEmpty ?? false)
            && (status?.outgoingErrors?.isEmpty ?? true)
            && (status?.outgoingFailedAccounts?.isEmpty ?? true)
    }
    func acknowledgeHome() {
        guard homeSnapshotComplete else { return }
        homeBaseline = homeOverview.stamps
        homeAcknowledgedAt = Date()
        if let data = try? JSONEncoder().encode(homeBaseline) {
            UserDefaults.standard.set(data, forKey: "homeOverviewBaseline")
            UserDefaults.standard.set(homeAcknowledgedAt, forKey: "homeOverviewAcknowledgedAt")
        }
    }

    var sentPRs: [SentPR] {
        (status?.outgoing ?? []).sorted {
            if $0.priority != $1.priority { return $0.priority < $1.priority }
            return ($0.requestedAt ?? $0.updatedAt, $0.id) < ($1.requestedAt ?? $1.updatedAt, $1.id)
        }
    }

    /// 나에게 할당된 열린 이슈, 긴급한 것부터. 정렬 점수는 봇이 정한다 (Jev → Priority 필드).
    var assignedIssues: [AssignedIssue] {
        (status?.issues ?? []).sorted(by: AssignedIssue.urgentFirst)
    }

    private var runsOffset: UInt64 = 0
    private var logOffset: UInt64 = 0
    private var sessionOffsets: [String: UInt64] = [:]
    private var sessionCache: [String: LiveSession] = [:]
    private var pollTask: Task<Void, Never>?
    private var statusPollTask: Task<Void, Never>?
    private var lastStatusFetch: Date?
    private var seenDrafts: Set<String>?
    private var seenPRs: Set<String>?

    // MARK: 수명

    func start() {
        guard pollTask == nil else { return }
        seenDrafts = UserDefaults.standard.stringArray(forKey: "seenDraftRevisions").map(Set.init)
        seenPRs = UserDefaults.standard.stringArray(forKey: "seenPRs").map(Set.init)
        Notifier.shared.prepare()
        readLaunchdInterval()
        if FileManager.default.fileExists(atPath: BotPaths.state.appending(path: "update-request.json").path) {
            Task {
                _ = await Shell.detach([BotPaths.executable.path, "update", "--apply-pending"], cwd: BotPaths.root.path)
            }
        }
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                self?.refreshLocal()
                if let self, !self.checkingUpdates,
                   Date().timeIntervalSince1970 - max(self.updateSnapshot?.checkedAt ?? 0,
                                                     self.lastUpdateCheckAttempt?.timeIntervalSince1970 ?? 0) > 86400 {
                    self.updateCheckTask = Task { [weak self] in await self?.checkUpdates() }
                }
                if let self, self.shouldRefetchStatus(), !self.isRefreshingStatus {
                    self.statusPollTask = Task { [weak self] in await self?.refreshStatus() }
                }
                try? await Task.sleep(for: .seconds(2))
            }
        }
    }

    func stop() {
        pollTask?.cancel(); pollTask = nil
        statusPollTask?.cancel(); statusPollTask = nil
        updateCheckTask?.cancel(); updateCheckTask = nil
    }

    private func shouldRefetchStatus() -> Bool {
        guard let last = lastStatusFetch else { return true }
        return Date().timeIntervalSince(last) > 60
    }

    // MARK: 로컬 파일

    /// 파일이 짧아졌으면(회전·재생성) 처음부터 다시 읽는다.
    /// 파일에 새로 붙은 부분.
    ///
    /// `restarted` 는 파일이 잘렸거나 비워졌거나 사라졌다는 뜻이다 — 갖고 있던
    /// 것을 버리고 처음부터 다시 읽어야 한다. 이것을 알려 주지 않으면, 이력을
    /// 지워 파일이 비어도 새로 붙은 바이트가 없다는 이유로 화면이 그대로 남는다.
    private struct Appended { var data: Data; var restarted: Bool }

    private func newBytes(_ url: URL, offset: inout UInt64) -> Appended? {
        guard let attrs = try? FileManager.default.attributesOfItem(atPath: url.path),
              let size = (attrs[.size] as? NSNumber)?.uint64Value else {
            // 파일이 사라졌다. 들고 있던 것이 있으면 비우라고 알린다.
            guard offset > 0 else { return nil }
            offset = 0
            return Appended(data: Data(), restarted: true)
        }
        var restarted = false
        if size < offset { offset = 0; restarted = true }
        guard size > offset else {
            return restarted ? Appended(data: Data(), restarted: true) : nil
        }
        guard let fh = try? FileHandle(forReadingFrom: url) else { return nil }
        defer { try? fh.close() }
        try? fh.seek(toOffset: offset)
        let data = (try? fh.readToEnd()) ?? Data()
        let from = offset
        offset = size
        return Appended(data: data, restarted: restarted || from == 0)
    }

    func readHomeTasks() {
        let path = BotPaths.state.appending(path: "home-tasks.json")
        guard FileManager.default.fileExists(atPath: path.path) else {
            if homeTaskData != nil { homeTasks = .empty; homeTaskData = nil }
            homeTaskError = nil
            return
        }
        guard let data = try? Data(contentsOf: path) else { homeTaskError = "할 일 기록을 읽지 못했습니다."; return }
        guard data != homeTaskData else { return }
        guard let tasks = try? JSONDecoder().decode(HomeTaskFile.self, from: data) else {
            homeTaskError = "할 일 기록을 읽지 못했습니다."; return
        }
        homeTaskData = data
        homeTasks = tasks
        homeTaskError = nil
    }
    func completeHomeTask(_ row: HomeRequest) async -> String {
        do {
            let old = homeTasks.currentTask(for: row.id)
            let task: HomeTask
            if let old, ["running", "reopened", "stopped"].contains(old.status) { task = old }
            else { task = try await HomeTaskClient.start(row, agent: "manual") }
            let result = await HomeTaskClient.command("complete", id: task.id)
            readHomeTasks()
            return result.code == 0 ? "완료 처리했습니다." : result.output
        } catch { return error.localizedDescription }
    }
    func reopenHomeTask(_ task: HomeTask) async {
        let result = await HomeTaskClient.command("reopen", id: task.id)
        readHomeTasks()
        if result.code != 0 { homeTaskError = result.output }
    }

    /// 지금 할 일에 보이는 작업 중 iTerm 탭에 연결된 것들이 아직 살아 있는지 본다.
    ///
    /// 탭을 닫거나 CLI 를 끝내도 파일에는 아무 흔적이 남지 않아, 앱은 계속 "작업 중"
    /// 으로 알고 있었다. 죽은 세션에 "작업 중" 인 작업은 `task stop` 으로 내려 "세션 종료 ·
    /// 완료 보고 없음" 이 되게 한다. 다시 열린 작업은 상태를 건드리지 않고 표시만 한다.
    func checkConnectedSessions() async {
        let links = homeTasks.current.values.compactMap { homeTasks.tasks[$0] }
            .filter { ["running", "reopened", "stopped"].contains($0.status) }
            .compactMap { task -> ExistingAgentSession.Link? in
                guard let terminalID = task.terminalID, let agent = AgentTerminal.Agent(rawValue: task.agent) else { return nil }
                return ExistingAgentSession.Link(taskID: task.id, agent: agent, terminalID: terminalID)
            }
        guard !links.isEmpty else { sessionLiveness = [:]; sessionCheckError = nil; return }
        do {
            let alive = try await ExistingAgentSession.liveness(links)
            sessionLiveness = alive
            sessionCheckError = nil
            let dead = alive.filter { !$0.value }.keys
                .compactMap { homeTasks.tasks[$0] }.filter { $0.status == "running" }
            for task in dead { _ = await HomeTaskClient.command("stop", id: task.id) }
            if !dead.isEmpty { readHomeTasks() }
        } catch {
            sessionCheckError = error.localizedDescription
        }
    }

    func refreshLocal() {
        readUpdates()
        readHomeTasks()
        readRuns()
        readLog()
        readReviewed()
        readFlags()
        readCurrent()          // 라이브 세션이 지금 실행의 시작 시각으로 지난 것을 거른다
        readLiveSessions()
        readMode()
        readDrafts()
        readQueue()
        readSchedule()
    }

    private func readQueue() {
        guard let data = try? Data(contentsOf: BotPaths.queue),
              let q = try? JSONDecoder().decode([QueuedReview].self, from: data) else {
            queue = []; return
        }
        queue = q
    }

    func queuePosition(_ repo: String, _ number: Int) -> Int? {
        QueuedReview.position(in: queue, repo: repo, number: number)
    }

    func prioritizeQueued(_ repo: String, _ number: Int) async {
        await run(["queue", "front", "\(repo)#\(number)"], label: "맨 앞으로")
    }

    func cancelQueued(_ repo: String, _ number: Int) async {
        await run(["queue", "remove", "\(repo)#\(number)"], label: "대기열에서 빼기")
    }

    private func readSchedule() {
        guard let data = try? Data(contentsOf: BotPaths.schedule),
              let s = try? JSONDecoder().decode(Schedule.self, from: data) else {
            schedule = nil; return
        }
        schedule = s
        if let secs = s.intervalSec, secs > 0 { tickInterval = TimeInterval(secs) }
    }

    private func readCurrent() {
        guard let data = try? Data(contentsOf: BotPaths.current),
              let c = try? JSONDecoder().decode(CurrentRun.self, from: data) else {
            current = nil; return
        }
        current = c
    }

    /// 게시 모드는 config.json 한 곳에서 나온다. 네트워크를 타는 status 를 기다리지
    /// 않고 여기서 바로 읽는다 — gh 가 죽어 있어도 토글은 살아 있어야 한다.
    private func readMode() {
        guard let data = try? Data(contentsOf: BotPaths.config),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return }
        let post = obj["post"] as? [String: Any]
        dryRunLocal = (post?["dryRun"] as? Bool) ?? dryRunLocal
    }

    /// 게시를 기다리는 초안. 파일을 직접 읽는다 — 틱이 초안을 떨구면 바로 뜬다.
    private func readDrafts() {
        guard let names = try? FileManager.default.contentsOfDirectory(atPath: BotPaths.drafts.path)
        else { drafts = []; return }
        let dec = JSONDecoder()
        drafts = names.filter { $0.hasSuffix(".json") }.compactMap { name in
            guard let data = try? Data(contentsOf: BotPaths.drafts.appending(path: name))
            else { return nil }
            return try? dec.decode(DraftPayload.self, from: data)
        }
        .sorted { ($0.at ?? "") > ($1.at ?? "") }
        announceNewDrafts()
    }

    /// PR 번호가 같아도 새로 생성한 초안은 다시 검토해야 한다.
    private func announceNewDrafts() {
        let ids = Set(drafts.map(\.notificationID))
        defer {
            seenDrafts = ids
            UserDefaults.standard.set(Array(ids), forKey: "seenDraftRevisions")
        }
        guard !autoPost,
              UserDefaults.standard.object(forKey: "notify") as? Bool ?? true else { return }
        for draft in DraftPayload.needingNotification(drafts, seen: seenDrafts ?? []) {
            Notifier.shared.post(
                title: "리뷰를 검토해 주세요",
                body: "\(draft.label) · \(draft.title ?? "")\n초안이 준비됐습니다. 검토 후 게시해 주세요.",
                draftID: draft.id)
        }
    }

    private func announceNewRequests(_ pending: [PendingPR]) {
        let ids = Set(pending.filter { $0.watched }.map(\.id))
        defer {
            seenPRs = ids
            UserDefaults.standard.set(Array(ids), forKey: "seenPRs")
        }
        guard let seen = seenPRs else { return }
        for id in ids.subtracting(seen) {
            guard let p = pending.first(where: { $0.id == id }) else { continue }
            let who = p.requestedBy.map { "\($0) 님이 요청" } ?? "리뷰 요청"
            announce("새 리뷰 요청", "\(p.label) · \(who)\n\(p.title)")
        }
    }

    private func announce(_ title: String, _ body: String) {
        guard UserDefaults.standard.object(forKey: "notify") as? Bool ?? true else { return }
        Notifier.shared.post(title: title, body: body)
    }

    private func readRuns() {
        guard let tail = newBytes(BotPaths.runsLog, offset: &runsOffset) else { return }
        if tail.restarted { runs.removeAll() }
        let dec = JSONDecoder()
        for line in tail.data.split(separator: UInt8(ascii: "\n")) where !line.isEmpty {
            if let r = try? dec.decode(RunRecord.self, from: Data(line)) { runs.append(r) }
        }
        runs.sort { ($0.at ?? "") > ($1.at ?? "") }
    }

    /// 보고 있는 날짜를 바꾼다. 파일이 다르므로 처음부터 다시 읽는다.
    func showLogDay(_ day: String) {
        guard day != logDay else { return }
        logDay = day
        logOffset = 0
        logLines.removeAll()
        readLog()
    }

    private func scanLogDays() {
        let fm = FileManager.default
        let names = (try? fm.contentsOfDirectory(atPath: BotPaths.logsDir.path)) ?? []
        logDays = names.filter { $0.hasSuffix(".log") && $0.count == 14 }
            .map { String($0.dropLast(4)) }
            .sorted(by: >)
    }

    private func readLog() {
        scanLogDays()
        guard let tail = newBytes(BotPaths.dayLog(logDay), offset: &logOffset) else { return }
        if tail.restarted { logLines.removeAll() }
        let data = tail.data
        let text = String(decoding: data, as: UTF8.self)
        for raw in text.split(separator: "\n", omittingEmptySubsequences: true) {
            let s = String(raw)
            let time = s.count >= 8 ? String(s.prefix(8)) : ""
            let body = s.count > 9 ? String(s.dropFirst(9)) : s
            let trimmed = body.trimmingCharacters(in: .whitespaces)
            logLines.append(LogLine(
                id: logLines.count, time: time, text: trimmed,
                indented: body.hasPrefix("  ") || body.hasPrefix("·"),
                isError: Self.looksLikeError(trimmed)))
        }
        if logLines.count > 2000 { logLines.removeFirst(logLines.count - 2000) }
    }

    /// 로그 한 줄이 에러인가.
    ///
    /// `—` 뒤는 모델이 쓴 자유 텍스트다 — 반증 사유·판정 사유·리뷰어 요약이 거기
    /// 실린다. 코드 리뷰 글에는 "실패 경로", "키무관 실패" 같은 말이 정상적으로
    /// 나오므로, 그 구간까지 훑으면 멀쩡한 기록이 빨갛게 칠해진다. 봇이 찍는
    /// 라벨은 항상 `—` 앞에 오므로 거기까지만 본다.
    static func looksLikeError(_ line: String) -> Bool {
        let head = line.components(separatedBy: " — ").first ?? line
        return head.contains("실패") || head.contains("초과")
            || head.contains("불일치") || head.contains("안전장치")
    }

    private func readReviewed() {
        guard let data = try? Data(contentsOf: BotPaths.reviewed),
              let map = try? JSONDecoder().decode([String: ReviewedRecord].self, from: data)
        else { return }
        reviewed = map
    }

    private func readFlags() {
        let fm = FileManager.default
        if fm.fileExists(atPath: BotPaths.paused.path),
           let text = try? String(contentsOf: BotPaths.paused, encoding: .utf8) {
            let lines = text.split(separator: "\n", omittingEmptySubsequences: false).map(String.init)
            paused = PausedInfo(at: lines.first ?? "", reason: lines.count > 1 ? lines[1] : "")
        } else {
            paused = nil
        }

        if fm.fileExists(atPath: BotPaths.lock.path),
           let text = try? String(contentsOf: BotPaths.lock, encoding: .utf8) {
            let parts = text.split(separator: " ").map(String.init)
            let pid = Int(parts.first ?? "") ?? 0
            let alive = pid > 0 && kill(pid_t(pid), 0) == 0
            running = RunningInfo(pid: pid,
                                  since: parts.count > 1 ? parts[1].trimmingCharacters(in: .newlines) : "",
                                  alive: alive)
        } else {
            running = nil
        }

        // 다음 틱 예상.
        //
        // 기준은 "마지막 리뷰" 가 아니라 "launchd 타이머가 마지막으로 다시 시작된
        // 순간" 이다 — 리뷰 기록을 기준으로 삼으면 몇 시간 전 리뷰 때문에 엉뚱한
        // 시각이 나온다. 손으로 부른 틱은 launchd 의 시계를 건드리지 않으므로
        // 카운트다운도 움직이지 않는 것이 맞다.
        guard let anchor = schedule?.anchor else { nextTick = nil; return }
        var candidate = anchor.addingTimeInterval(tickInterval)
        // 잠들었다 깨면 지난 발사가 밀린다 — 다음 경계까지 굴린다.
        while candidate <= Date() { candidate.addTimeInterval(tickInterval) }
        nextTick = candidate
    }

    private func todayAt(_ hhmmss: String) -> Date? {
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd HH:mm:ss"
        let today = DateFormatter()
        today.dateFormat = "yyyy-MM-dd"
        return f.date(from: "\(today.string(from: Date())) \(hhmmss)")
    }

    private func readLaunchdInterval() {
        Task { [weak self] in
            let out = await Shell.run(["/bin/launchctl", "print",
                                       "gui/\(getuid())/com.review-bot.agent"]).output
            guard let range = out.range(of: "run interval = ") else { return }
            let tail = out[range.upperBound...].prefix(while: { $0.isNumber })
            if let secs = TimeInterval(tail), secs > 0 { self?.tickInterval = secs }
        }
    }

    // MARK: 살아 있는 세션

    private func readLiveSessions() {
        let fm = FileManager.default
        guard let wts = try? fm.contentsOfDirectory(atPath: BotPaths.worktrees.path) else {
            live = []; return
        }
        var found: [LiveSession] = []
        for wt in wts {
            let wtPath = BotPaths.worktrees.appending(path: wt).path
            let dir = BotPaths.sessionDir(forWorktree: wtPath)
            guard let files = try? fm.contentsOfDirectory(atPath: dir.path) else { continue }
            let cutoff = LiveSession.cutoff(worktree: wt, current: current)
            for file in files where file.hasSuffix(".jsonl") {
                let url = dir.appending(path: file)
                // 지난 실행의 transcript 는 읽지도 않는다 — PR 한 건에 수십 MB 가 남아 있다.
                if let cutoff,
                   let modified = try? fm.attributesOfItem(atPath: url.path)[.modificationDate] as? Date,
                   modified < cutoff { continue }
                if let s = readSession(url, pr: wt) { found.append(s) }
            }
        }
        live = LiveSession.latestPerTask(found)
    }

    private func readSession(_ url: URL, pr: String) -> LiveSession? {
        var offset = sessionOffsets[url.path] ?? 0
        let fresh = newBytes(url, offset: &offset)
        sessionOffsets[url.path] = offset
        guard let tail = fresh else { return sessionCache[url.path] }
        let data = tail.data
        // 세션 파일이 새로 시작됐으면 쌓아 둔 턴 수는 의미가 없다.
        if tail.restarted { sessionCache[url.path] = nil }

        var s = sessionCache[url.path] ?? LiveSession(
            id: url.path, agent: SessionLabel.pending, pr: pr.replacingOccurrences(of: "__", with: "/"),
            turns: 0, tools: [:], lastTool: "", lastDetail: "",
            updatedAt: Date(), deniedTools: 0)

        for line in data.split(separator: UInt8(ascii: "\n")) where !line.isEmpty {
            guard let obj = try? JSONSerialization.jsonObject(with: Data(line)) as? [String: Any]
            else { continue }
            s.turns += 1
            guard let message = obj["message"] as? [String: Any] else { continue }
            if s.agent == SessionLabel.pending, obj["type"] as? String == "user",
               let content = message["content"] as? String,
               let name = SessionLabel.parse(content) { s.agent = name }
            guard let blocks = message["content"] as? [[String: Any]] else { continue }
            for b in blocks {
                switch b["type"] as? String {
                case "tool_use":
                    let name = (b["name"] as? String) ?? "?"
                    s.tools[name, default: 0] += 1
                    s.lastTool = name
                    s.lastDetail = Self.detail(of: b["input"] as? [String: Any])
                case "tool_result":
                    if (b["is_error"] as? Bool) == true { s.deniedTools += 1 }
                default: break
                }
            }
            if s.agent == SessionLabel.pending, obj["type"] as? String == "user",
               let blocks = message["content"] as? [[String: Any]] {
                for b in blocks where b["type"] as? String == "text" {
                    if let name = SessionLabel.parse((b["text"] as? String) ?? "") { s.agent = name }
                }
            }
        }
        s.updatedAt = (try? FileManager.default.attributesOfItem(atPath: url.path)[.modificationDate] as? Date) ?? Date()
        sessionCache[url.path] = s
        return s
    }

    private static func detail(of input: [String: Any]?) -> String {
        guard let input else { return "" }
        for key in ["file_path", "pattern", "path", "command", "prompt"] {
            if let v = input[key] as? String, !v.isEmpty {
                let short = v.replacingOccurrences(of: BotPaths.worktrees.path + "/", with: "")
                return short.count > 90 ? String(short.prefix(90)) + "…" : short
            }
        }
        return ""
    }

    // MARK: 봇 호출

    func refreshStatus() async {
        guard !isRefreshingStatus else { return }
        isRefreshingStatus = true
        defer { isRefreshingStatus = false }
        lastStatusFetch = Date()
        let result = await Shell.run([BotPaths.executable.path, "status", "--json"],
                                     cwd: BotPaths.root.path)
        guard let data = result.output.data(using: .utf8),
              var snap = try? JSONDecoder().decode(StatusSnapshot.self, from: data) else {
            statusError = result.output.isEmpty ? "status 응답 없음" : String(result.output.prefix(300))
            return
        }
        // 일부 계정 조회 실패는 빈 목록으로 보이지 않게 이전 결과를 유지한다.
        let failed = Set(snap.outgoingFailedAccounts ?? [])
        snap.outgoing = (snap.outgoing ?? []) + (status?.outgoing ?? []).filter { failed.contains($0.account) }
        snap.selectionSettings = .local
        status = snap
        statusError = snap.errors.first
        statusLoadedAt = Date()
        readHomeTasks()
        await checkConnectedSessions()
        if homeBaseline == nil && homeSnapshotComplete { acknowledgeHome() }
        if let secs = snap.config?.intervalSec, secs > 0 { tickInterval = TimeInterval(secs) }
        announceNewRequests(snap.pending)
    }

    /// 오래 걸리는 작업(틱·리뷰)은 앱에서 떼어 내 띄운다.
    ///
    /// 자식으로 붙여 두면 앱이 꺼질 때 stdout 파이프가 끊기고, 그 다음 로그 한 줄에
    /// 리뷰가 통째로 죽는다 — 실제로 20분짜리 리뷰 한 건을 그렇게 잃었다. 떼어 내면
    /// 앱을 껐다 켜도 리뷰는 계속 돌고, 출력은 봇이 state/bot.log 에 남긴다.
    func runDetached(_ args: [String], label: String) async {
        guard busy == nil else { return }
        busy = label
        defer { busy = nil }
        _ = await Shell.detach([BotPaths.executable.path] + args, cwd: BotPaths.root.path)
        try? await Task.sleep(for: .seconds(2))   // lock 이 잡히는 것을 보고 나간다
        refreshLocal()
    }

    /// 봇 커맨드 하나를 돌리고 끝나면 상태를 다시 읽는다. 금방 끝나는 것만 여기로.
    @discardableResult
    func run(_ args: [String], label: String) async -> Shell.Result? {
        guard busy == nil else { return nil }
        busy = label
        defer { busy = nil }
        let result = await Shell.run([BotPaths.executable.path] + args, cwd: BotPaths.root.path)
        lastCommandOutput = result.output.trimmingCharacters(in: .whitespacesAndNewlines)
        refreshLocal()
        await refreshStatus()
        return result
    }

    // MARK: 집계

    /// 저장소 → PR 로 묶은 이력. 저장소는 이름순, PR 은 번호 큰 것부터.
    var runGroups: [RunGroup] {
        var buckets: [String: [RunRecord]] = [:]
        for r in runs {
            guard let repo = r.repo, let n = r.number else { continue }
            buckets["\(repo)#\(n)", default: []].append(r)
        }
        return buckets.map { key, rs in
            let repo = String(key.split(separator: "#").first ?? "")
            let n = Int(key.split(separator: "#").last.map(String.init) ?? "") ?? 0
            return RunGroup(id: key, repo: repo, number: n,
                            runs: rs.sorted { ($0.at ?? "") > ($1.at ?? "") })
        }
        .sorted { a, b in a.repo == b.repo ? a.number > b.number : a.repo < b.repo }
    }

    var todaysRuns: [RunRecord] {
        let today = DateFormatter()
        today.dateFormat = "yyyy-MM-dd"
        let prefix = today.string(from: Date())
        return runs.filter { ($0.at ?? "").hasPrefix(prefix) }
    }

    /// 자동 게시인가. status 를 아직 못 읽었으면 보수적으로 "검토 후" 로 본다.
    var autoPost: Bool {
        if let local = dryRunLocal { return !local }
        return status.map { !$0.dryRun } ?? false
    }

    func setAutoPost(_ on: Bool) async {
        await run(["mode", on ? "auto" : "draft"], label: on ? "자동 게시로 전환" : "검토 후 게시로 전환")
    }

    func setInterval(minutes: Int) async {
        await run(["interval", String(minutes)], label: "틱 주기 변경")
    }

    /// 틱당 리뷰 상한. launchd 를 건드리지 않으므로 틱이 도는 중에도 받아 준다 —
    /// 도는 틱은 이전 상한으로 끝내고 다음 틱부터 새 값이 적용된다.
    func setInsights(_ on: Bool) async {
        await run(["insights", on ? "enable" : "disable"], label: "요청 분석 설정")
    }

    func selectionRegistrationDidChange() {
        status?.selectionSettings = .local
    }

    func checkJev(_ input: JevAPI.Input) async -> String {
        let output = await JevAPI.run(input: input)
        if output["ok"] as? Bool == true { return "Jev 연결과 분류 응답을 확인했습니다." }
        if let status = output["status"] as? Int { return "Jev API HTTP \(status) · 키와 모델을 확인해 주세요." }
        return "Jev 연결을 확인하지 못했습니다. 등록 상태와 키체인 접근을 확인해 주세요."
    }

    func setInsightWorkspace(_ workspace: String) async {
        await run(["insights", "workspace", workspace], label: "Workspace 설정")
    }

    func checkInsightConnection() async {
        await run(["insights", "check"], label: "Claude API 연결 확인")
    }

    func setInsightModel(_ model: String) async {
        await run(["insights", "model", model], label: "분석 모델 변경")
    }

    func setMaxPerTick(_ n: Int) async {
        await run(["cap", String(n)], label: "틱당 리뷰 상한 변경")
    }

    var config: BotConfig? { status?.config }

    // MARK: 리뷰어·저장소·계정 관리
    //
    // 전부 CLI 로 넘긴다. 설정을 고치는 규칙(이름 검증, 필수 섹션, 토큰 주인 대조)이
    // 한 곳에만 있어야 앱에서 바꾸든 터미널에서 바꾸든 같은 결과가 나온다.

    func addAgent(path: String) async {
        await run(["agent", "add", path], label: "리뷰어 등록")
    }

    func removeAgent(_ name: String) async {
        await run(["agent", "remove", name], label: "리뷰어 해제")
    }

    func setAgentModel(_ name: String, _ model: String) async {
        await run(["agent", "model", name, "--model", model], label: "\(name) 모델 변경")
    }

    func addRepo(_ slug: String, path: String, account: String) async {
        var args = ["repo", "add", slug]
        if !path.isEmpty { args += ["--path", path] }
        if !account.isEmpty { args += ["--account", account] }
        await run(args, label: "저장소 등록")
    }

    func removeRepo(_ slug: String) async {
        await run(["repo", "remove", slug], label: "저장소 해제")
    }

    func setRepoEnabled(_ slug: String, _ on: Bool) async {
        await run(["repo", on ? "enable" : "disable", slug], label: "저장소 \(on ? "켜기" : "끄기")")
    }

    func setAccount(_ name: String) async {
        await run(["account", "set", name], label: "계정 설정")
    }

    func clearAccount() async {
        await run(["account", "clear"], label: "계정 지정 해제")
    }

    /// `review-bot` 의 PUBLISH_STALE — 초안 이후 새 커밋이 올라와 게시를 거절했다.
    static let publishStaleExit: Int32 = 3

    /// 새 커밋 때문에 게시가 실패한 초안. 화면이 이것을 보고 다시 리뷰할지 묻는다.
    var stalePublish: DraftPayload?

    func publish(_ draft: DraftPayload) async {
        let result = await run(["publish", "\(draft.repo)#\(draft.number)"], label: "초안 게시")
        if result?.code == Self.publishStaleExit { stalePublish = draft }
    }


    /// 초안의 이벤트만 바꾼다. 파일만 고치므로 GitHub 에는 아무것도 나가지 않는다.
    func setDraftEvent(_ draft: DraftPayload, _ event: String) async {
        await run(["drafts", "event", "\(draft.repo)#\(draft.number)", event],
                  label: "\(eventLabel(event))(으)로 변경")
    }

    func tickNow() async { await runDetached(["tick"], label: "지금 틱") }

    // 이력 삭제. 처리 지문(reviewed.json)은 건드리지 않으므로 지웠다고 다시
    // 리뷰하지는 않는다 — 보기 싫은 기록만 치우는 것이다.
    func forget(_ run: RunRecord) async {
        guard let repo = run.repo, let n = run.number, let at = run.at else { return }
        await self.run(["forget", "\(repo)#\(n)", "--at", at], label: "기록 삭제")
    }

    func forget(group: RunGroup) async {
        await run(["forget", "\(group.repo)#\(group.number)"], label: "이력 삭제")
    }

    func cleanPR(_ repo: String, _ number: Int) async {
        await run(["forget", "\(repo)#\(number)", "--artifacts"], label: "PR 로컬 자료 정리")
    }

    func forgetAll() async {
        await run(["forget", "--all"], label: "이력 전체 삭제")
    }

    /// 봇이 쉬고 있으면 바로 돌고, 다른 PR 을 보는 중이면 대기열에 들어간다.
    func review(_ repo: String, _ number: Int) async {
        await runDetached(["review", "\(repo)#\(number)"], label: "재리뷰")
    }

    enum Health { case running, paused, idle, problem }

    /// 이 PR 의 초안을 지금 건드리면 안 되는가. 봇이 다른 PR 을 보는 중이면 false 다.
    func draftLocked(_ repo: String, _ number: Int) -> Bool {
        DraftGate.blocked(busy: busy != nil, running: health == .running,
                          current: current, repo: repo, number: number)
    }

    var health: Health {
        if let r = running, r.alive { return .running }
        if paused != nil { return .paused }
        if let latest = runs.first, latest.failed || latest.partial { return .problem }
        if statusError != nil { return .problem }
        return .idle
    }
}

// MARK: - 프로세스 실행

enum Shell {
    struct Result: Sendable { var code: Int32; var output: String }

    /// 앱 수명에서 떼어 내 띄운다. 파이프를 걸지 않으므로 앱이 꺼져도 SIGPIPE 가 없다.
    static func detach(_ args: [String], cwd: String? = nil) async -> Result {
        let quoted = args
            .map { "'" + $0.replacingOccurrences(of: "'", with: "'\\''") + "'" }
            .joined(separator: " ")
        return await run(["/bin/sh", "-c", "nohup \(quoted) >/dev/null 2>&1 &"], cwd: cwd)
    }

    /// 메인 스레드를 막지 않도록 항상 백그라운드에서 돈다.
    static func run(_ args: [String], cwd: String? = nil) async -> Result {
        await Task.detached(priority: .userInitiated) {
            let p = Process()
            p.executableURL = URL(fileURLWithPath: args[0])
            p.arguments = Array(args.dropFirst())
            if let cwd { p.currentDirectoryURL = URL(fileURLWithPath: cwd) }
            var env = ProcessInfo.processInfo.environment
            env["PATH"] = "/Users/\(NSUserName())/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
            p.environment = env
            let pipe = Pipe()
            p.standardOutput = pipe
            p.standardError = pipe
            do { try p.run() } catch {
                return Result(code: -1, output: "실행 실패: \(error.localizedDescription)")
            }
            let data = (try? pipe.fileHandleForReading.readToEnd()) ?? Data()
            p.waitUntilExit()
            return Result(code: p.terminationStatus, output: String(decoding: data, as: UTF8.self))
        }.value
    }
}

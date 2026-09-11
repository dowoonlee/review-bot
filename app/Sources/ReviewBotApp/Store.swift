import Foundation
import Observation

/// 봇의 상태를 한곳에 모은다.
///
/// 로컬 파일(로그·이력·lock·PAUSED)은 2초마다 증분으로 읽고, gh 조회가 필요한
/// `status --json` 은 네트워크를 타므로 60초마다 또는 사용자가 새로고침할 때만 부른다.
@MainActor
@Observable
final class BotStore {
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
    var schedule: Schedule?

    var paused: PausedInfo?
    var running: RunningInfo?
    var nextTick: Date?
    var tickInterval: TimeInterval = 900
    var lastCommandOutput: String = ""
    var busy: String?               // 실행 중인 명령 이름
    var statusError: String?
    var statusLoadedAt: Date?

    private var runsOffset: UInt64 = 0
    private var logOffset: UInt64 = 0
    private var sessionOffsets: [String: UInt64] = [:]
    private var sessionCache: [String: LiveSession] = [:]
    private var pollTask: Task<Void, Never>?
    private var lastStatusFetch: Date?
    private var seenDrafts: Set<String>?
    private var seenPRs: Set<String>?

    // MARK: 수명

    func start() {
        guard pollTask == nil else { return }
        seenDrafts = UserDefaults.standard.stringArray(forKey: "seenDrafts").map(Set.init)
        seenPRs = UserDefaults.standard.stringArray(forKey: "seenPRs").map(Set.init)
        Notifier.shared.prepare()
        readLaunchdInterval()
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                self?.refreshLocal()
                if let self, self.shouldRefetchStatus() { await self.refreshStatus() }
                try? await Task.sleep(for: .seconds(2))
            }
        }
    }

    func stop() { pollTask?.cancel(); pollTask = nil }

    private func shouldRefetchStatus() -> Bool {
        guard let last = lastStatusFetch else { return true }
        return Date().timeIntervalSince(last) > 60
    }

    // MARK: 로컬 파일

    /// 파일이 짧아졌으면(회전·재생성) 처음부터 다시 읽는다.
    private func newBytes(_ url: URL, offset: inout UInt64) -> Data? {
        guard let attrs = try? FileManager.default.attributesOfItem(atPath: url.path),
              let size = (attrs[.size] as? NSNumber)?.uint64Value else { return nil }
        if size < offset { offset = 0 }
        guard size > offset else { return nil }
        guard let fh = try? FileHandle(forReadingFrom: url) else { return nil }
        defer { try? fh.close() }
        try? fh.seek(toOffset: offset)
        let data = (try? fh.readToEnd()) ?? Data()
        offset = size
        return data.isEmpty ? nil : data
    }

    func refreshLocal() {
        readRuns()
        readLog()
        readReviewed()
        readFlags()
        readLiveSessions()
        readDrafts()
        readMode()
        readCurrent()
        readSchedule()
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

    /// 처음 본 초안만 알린다. 이미 본 것은 UserDefaults 에 남아 재시작해도 조용하다.
    private func announceNewDrafts() {
        let ids = Set(drafts.map(\.id))
        defer {
            seenDrafts = ids
            UserDefaults.standard.set(Array(ids), forKey: "seenDrafts")
        }
        guard let seen = seenDrafts else { return }     // 첫 관측은 기준선일 뿐이다
        for id in ids.subtracting(seen) {
            guard let d = drafts.first(where: { $0.id == id }) else { continue }
            announce("리뷰 초안이 준비됐습니다",
                     "\(d.label) · \(d.event ?? "COMMENT") · 인라인 \(d.inline ?? 0)건 — 읽어 보고 게시하세요")
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
        guard let data = newBytes(BotPaths.runsLog, offset: &runsOffset) else { return }
        if runsOffset == UInt64(data.count) { runs.removeAll() }   // 처음부터 읽은 경우
        let dec = JSONDecoder()
        for line in data.split(separator: UInt8(ascii: "\n")) where !line.isEmpty {
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
        guard let data = newBytes(BotPaths.dayLog(logDay), offset: &logOffset) else { return }
        if logOffset == UInt64(data.count) { logLines.removeAll() }
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
            for file in files where file.hasSuffix(".jsonl") {
                let url = dir.appending(path: file)
                if let s = readSession(url, pr: wt) { found.append(s) }
            }
        }
        live = found.sorted { $0.updatedAt > $1.updatedAt }
    }

    private func readSession(_ url: URL, pr: String) -> LiveSession? {
        var offset = sessionOffsets[url.path] ?? 0
        let fresh = newBytes(url, offset: &offset)
        sessionOffsets[url.path] = offset
        guard let data = fresh else { return sessionCache[url.path] }

        var s = sessionCache[url.path] ?? LiveSession(
            id: url.path, agent: "?", pr: pr.replacingOccurrences(of: "__", with: "/"),
            turns: 0, tools: [:], lastTool: "", lastDetail: "",
            updatedAt: Date(), deniedTools: 0)

        for line in data.split(separator: UInt8(ascii: "\n")) where !line.isEmpty {
            guard let obj = try? JSONSerialization.jsonObject(with: Data(line)) as? [String: Any]
            else { continue }
            s.turns += 1
            guard let message = obj["message"] as? [String: Any] else { continue }
            if s.agent == "?", let content = message["content"] as? String,
               let name = Self.agentName(in: content) { s.agent = name }
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
            if s.agent == "?", let blocks = message["content"] as? [[String: Any]] {
                for b in blocks where b["type"] as? String == "text" {
                    if let name = Self.agentName(in: (b["text"] as? String) ?? "") { s.agent = name }
                }
            }
        }
        s.updatedAt = (try? FileManager.default.attributesOfItem(atPath: url.path)[.modificationDate] as? Date) ?? Date()
        sessionCache[url.path] = s
        return s
    }

    /// 프롬프트 첫머리의 "당신은 **code-reviewer** 입니다" 에서 이름을 집어낸다.
    private static func agentName(in text: String) -> String? {
        guard let open = text.range(of: "**"),
              let close = text.range(of: "**", range: open.upperBound..<text.endIndex)
        else { return nil }
        let name = String(text[open.upperBound..<close.lowerBound])
        return name.contains("reviewer") || name.contains("auditor") ? name : nil
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
        lastStatusFetch = Date()
        let result = await Shell.run([BotPaths.executable.path, "status", "--json"],
                                     cwd: BotPaths.root.path)
        guard let data = result.output.data(using: .utf8),
              let snap = try? JSONDecoder().decode(StatusSnapshot.self, from: data) else {
            statusError = result.output.isEmpty ? "status 응답 없음" : String(result.output.prefix(300))
            return
        }
        status = snap
        statusError = snap.errors.first
        statusLoadedAt = Date()
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
    func run(_ args: [String], label: String) async {
        guard busy == nil else { return }
        busy = label
        defer { busy = nil }
        let result = await Shell.run([BotPaths.executable.path] + args, cwd: BotPaths.root.path)
        lastCommandOutput = result.output.trimmingCharacters(in: .whitespacesAndNewlines)
        refreshLocal()
        await refreshStatus()
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

    func publish(_ draft: DraftPayload) async {
        await run(["publish", "\(draft.repo)#\(draft.number)"], label: "초안 게시")
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

    func forgetAll() async {
        await run(["forget", "--all"], label: "이력 전체 삭제")
    }

    func review(_ repo: String, _ number: Int) async {
        await runDetached(["review", "\(repo)#\(number)"], label: "재리뷰")
    }

    enum Health { case running, paused, idle, problem }

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

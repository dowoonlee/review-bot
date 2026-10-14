import SwiftUI
import Security

struct SelectionSettings: Decodable, Sendable {
    var enabled: Bool
    var model: String
    var registration: String
    var keyReady: Bool
    static var local: SelectionSettings {
        .init(enabled: JevRegistration.enabled, model: JevRegistration.model,
              registration: JevRegistration.generation, keyReady: InsightKeychain.accessVerified(for: JevRegistration.service))
    }
}
struct JevDecision: Decodable, Sendable {
    var status: String
    var actor: String?
    var action: String?
    var urgency: Double?
    var confidence: Double?
    var analyzedAt: String?
    var error: String?
    var usable: Bool { status == "ready" && (confidence ?? 0) >= 0.8 }
    var label: String {
        guard usable else { return status == "error" ? "Jev 오류 · 기존 판단 유지" : "Jev 확인 필요 · 기존 판단 유지" }
        switch actor {
        case "self": return "Jev: 내가 대응할 차례"
        case "other": return "Jev: 상대 응답 대기 후보"
        case "none": return "Jev: 추가 대응 불필요 후보"
        default: return "Jev: 대응 주체 확인 필요"
        }
    }
}

struct JevSettingsView: View {
    @Environment(BotStore.self) private var store
    @State private var model = JevRegistration.model.isEmpty ? "jev-latest" : JevRegistration.model
    @State private var apiKey = ""
    @State private var busy = false
    @State private var message = ""
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("선택·분류 모델 (Jev)").f(.headline)
                Spacer()
                Text(JevRegistration.enabled ? "ON · 등록됨" : "OFF · 미등록 또는 키 접근 필요")
                    .f(.caption).foregroundStyle(JevRegistration.enabled ? Color.green : Color.secondary)
            }
            Text("모델과 API 키를 등록하면 자동으로 켜집니다. 등록을 해제하면 기존 정렬로 돌아갑니다. 감시 PR의 대화와 분석 내용을 TypeSafe API에 보내 대응 주체와 우선순위를 평가합니다.")
                .f(.caption).foregroundStyle(.secondary)
            TextField("모델 ID", text: $model).textFieldStyle(.roundedBorder)
            SecureField("TypeSafe API 키 · 교체할 때만 다시 입력", text: $apiKey).textFieldStyle(.roundedBorder)
            HStack {
                Button("모델 등록 / 갱신") {
                    busy = true
                    let value = apiKey.trimmingCharacters(in: .whitespacesAndNewlines)
                    let selected = model
                    apiKey = ""
                    Task {
                        let saved = value.isEmpty ? (InsightKeychain.accessVerified(for: JevRegistration.service) ? errSecSuccess : errSecItemNotFound) :
                            await Task.detached { InsightKeychain.save(value, serviceName: JevRegistration.service) }.value
                        if saved == errSecSuccess {
                            JevRegistration.register(model: selected)
                            message = "등록했습니다. 다음 틱부터 Jev 분류가 켜집니다."
                        } else { message = "키 저장 실패 (\(saved)). 모델을 새로 등록하지 않았습니다." }
                        store.selectionRegistrationDidChange()
                        busy = false
                        await store.refreshStatus()
                    }
                }.disabled(!JevRegistration.validModel(model) || (!apiKey.isEmpty && !JevAPI.validKey(apiKey.trimmingCharacters(in: .whitespacesAndNewlines))) || (apiKey.isEmpty && !InsightKeychain.accessVerified(for: JevRegistration.service)))
                Button("등록 해제 · 키 삭제", role: .destructive) {
                    JevRegistration.remove()
                    store.selectionRegistrationDidChange()
                    busy = true
                    apiKey = ""
                    Task {
                        let status = await Task.detached { InsightKeychain.delete(serviceName: JevRegistration.service) }.value
                        InsightKeychain.recordAccess(errSecItemNotFound, serviceName: JevRegistration.service)
                        message = status == errSecSuccess || status == errSecItemNotFound ? "등록을 해제했습니다. Jev 분류가 꺼졌습니다." : "기능은 꺼졌습니다. 키 삭제는 완료하지 못했습니다 (\(status))."
                        store.selectionRegistrationDidChange()
                        busy = false
                        await store.refreshStatus()
                    }
                }
            }
            HStack {
                Button("키체인 접근 확인") {
                    busy = true
                    Task {
                        let result = await Task.detached { InsightKeychain.authorize(serviceName: JevRegistration.service) }.value
                        message = result == errSecSuccess ? "Jev 키 접근을 확인했습니다." : "키 접근 실패 (\(result)). 자동 실행에서는 암호 창을 열지 않습니다."
                        store.selectionRegistrationDidChange()
                        busy = false
                        await store.refreshStatus()
                    }
                }
                Button("연결 확인 · 1회 호출") {
                    busy = true
                    Task {
                        let input = JevAPI.Input(action: "check", model: JevRegistration.model, registration: JevRegistration.generation)
                        let output = await store.checkJev(input)
                        message = output
                        busy = false
                    }
                }.disabled(!JevRegistration.enabled)
                if busy { ProgressView().controlSize(.small) }
            }
            Text("키는 Claude 키와 별도의 키체인 항목에 보관합니다. 자동 실행은 암호 창을 열지 않으며, 불확실한 분류는 기존 판단을 유지합니다.")
                .f(.caption).foregroundStyle(.secondary)
            if !message.isEmpty { Text(message).f(.caption) }
        }
        .disabled(busy)
        .onDisappear { apiKey = "" }
    }
}

import Foundation

/// What this Mac keeps on disk: one secret per paired Mac, in the same file the zero-install Python client uses.
struct Credentials: Codable {
    var server: String = ""
    var url: String = ""
    var device_id: String = ""
    var name: String = ""
    var secret: String = ""
}

/// `~/.config/jarvis-remote`: `credentials.json` (the secret) and `server` (the URL the installer baked in).
enum RemoteFiles {
    /// `JARVIS_REMOTE_DIR` moves it, which is what the tests use so a real pairing is never touched.
    static let dir: URL = {
        if let override = ProcessInfo.processInfo.environment["JARVIS_REMOTE_DIR"], !override.isEmpty {
            return URL(fileURLWithPath: override, isDirectory: true)
        }
        return FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".config/jarvis-remote", isDirectory: true)
    }()
    static let credentials = dir.appendingPathComponent("credentials.json")
    static let server = dir.appendingPathComponent("server")

    static func loadCredentials() -> Credentials? {
        guard let data = try? Data(contentsOf: credentials),
              let record = try? JSONDecoder().decode(Credentials.self, from: data),
              !record.secret.isEmpty
        else { return nil }
        return record
    }

    static func saveCredentials(_ record: Credentials) throws {
        try FileManager.default.createDirectory(
            at: dir, withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700]
        )
        let data = try JSONEncoder().encode(record)
        try data.write(to: credentials, options: .atomic)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: credentials.path)
    }

    static func deleteCredentials() {
        try? FileManager.default.removeItem(at: credentials)
    }

    /// The installer's file, so first launch already knows which Mac to talk to.
    static func savedServer() -> String {
        (try? String(contentsOf: server, encoding: .utf8))?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
    }
}

/// `mac-mini.local`, `http://100.72.83.99:8643` and `100.72.83.99` all become `http://<host>:<port>`.
func normalizeServer(_ raw: String) -> String? {
    var text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
    guard !text.isEmpty else { return nil }
    if !text.contains("://") { text = "http://" + text }
    guard var parts = URLComponents(string: text),
          let scheme = parts.scheme?.lowercased(), scheme == "http" || scheme == "https",
          let host = parts.host, !host.isEmpty
    else { return nil }
    parts.scheme = scheme
    if parts.port == nil { parts.port = 8643 }
    parts.path = ""
    parts.query = nil
    parts.fragment = nil
    return parts.string.map { $0.hasSuffix("/") ? String($0.dropLast()) : $0 }
}

struct Health {
    var version: String
    var pid: Int
}

struct ApiError: LocalizedError {
    var status: Int
    var message: String
    var errorDescription: String? { message }
}

/// The three calls this app makes: is Jarvis there, may I have a code, and here is the code.
struct JarvisApi {
    let server: String

    private func request(_ path: String, method: String = "GET", body: [String: String]? = nil, token: String? = nil) -> URLRequest? {
        guard let url = URL(string: server + path) else { return nil }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.timeoutInterval = 15
        if let token { request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try? JSONEncoder().encode(body)
        }
        return request
    }

    private func send(_ request: URLRequest) async throws -> Data {
        let (data, response) = try await URLSession.shared.data(for: request)
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard (200..<300).contains(status) else {
            let detail = (try? JSONDecoder().decode([String: String].self, from: data))?["error"]
            throw ApiError(status: status, message: detail ?? "Jarvis answered \(status).")
        }
        return data
    }

    func health() async throws -> Health {
        guard let request = request("/health") else { throw ApiError(status: 0, message: "That server address is not a URL.") }
        let data = try await send(request)
        guard let body = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw ApiError(status: 0, message: "Jarvis answered something unreadable.")
        }
        return Health(
            version: body["version"] as? String ?? "unknown",
            pid: body["pid"] as? Int ?? 0
        )
    }

    /// Asks Jarvis to show a code on `jarvis top`. The answer never carries the code itself.
    @discardableResult
    func requestCode(name: String) async throws -> Int {
        guard let request = request("/v1/pair/request", method: "POST", body: ["name": name]) else {
            throw ApiError(status: 0, message: "That server address is not a URL.")
        }
        let data = try await send(request)
        let body = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        return body?["expires_in"] as? Int ?? 300
    }

    func pair(code: String, name: String) async throws -> Credentials {
        guard let request = request("/v1/pair", method: "POST", body: ["code": code, "name": name]) else {
            throw ApiError(status: 0, message: "That server address is not a URL.")
        }
        let data = try await send(request)
        guard let body = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let secret = body["secret"] as? String,
              let device = body["device"] as? [String: Any]
        else { throw ApiError(status: 0, message: "Jarvis paired this Mac but sent no secret.") }
        let socket = server.hasPrefix("https") ? "wss" + server.dropFirst(4) : "ws" + server.dropFirst(4)
        return Credentials(
            server: server,
            url: socket + "/ws",
            device_id: device["id"] as? String ?? "",
            name: device["name"] as? String ?? name,
            secret: secret
        )
    }

    /// Does this Mac's secret still work? `GET /v1/status` accepts a paired device, so `nil` means the network failed.
    func secretWorks(_ secret: String) async -> Bool? {
        guard let request = request("/v1/status", token: secret) else { return nil }
        do {
            _ = try await send(request)
            return true
        } catch let error as ApiError where error.status == 401 || error.status == 403 {
            return false
        } catch {
            return nil
        }
    }
}

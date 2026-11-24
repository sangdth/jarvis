import AppKit
import Foundation
import SwiftUI

/// One screen, two states: connect (which is also "pair" the first time), and settings.
@MainActor
final class AppModel: ObservableObject {
    enum Phase: Equatable {
        case idle            // nothing running: the button pairs, or connects
        case requesting      // asked Jarvis for a code
        case pairing         // waiting for the six digits
        case connecting
        case connected
        case failed(String)
    }

    @Published private(set) var phase: Phase = .idle
    @Published private(set) var paired = false
    @Published private(set) var deviceName = ""
    @Published private(set) var detail = ""
    @Published private(set) var serverVersion = "—"
    @Published private(set) var earActive = false
    @Published private(set) var updating = false
    @Published var serverURL: String
    @Published var codeInput = ""
    @Published var message: String?
    @Published var showsSettings = false

    let appVersion = Bundle.main.object(forInfoDictionaryKey: "JarvisVersion") as? String ?? "dev"

    private var credentials = RemoteFiles.loadCredentials()
    private var relay: AudioRelay?
    private var attempt = 0
    private var pairingInFlight = false

    init() {
        paired = credentials != nil
        deviceName = credentials?.name ?? ""
        serverURL = credentials?.server ?? RemoteFiles.savedServer()
        // Ask the server what it is running even before pairing: `GET /health` needs no secret, and Update works then.
        if !serverURL.isEmpty { Task { await refresh() } }
    }

    // MARK: - What the screen shows

    var server: String? { normalizeServer(serverURL) }

    var buttonLabel: String {
        switch phase {
        case .idle: return paired ? "Connect" : "Connect"
        case .requesting: return "Asking…"
        case .pairing: return "Pair"
        case .connecting: return "Connecting…"
        case .connected: return "Disconnect"
        case .failed: return "Retry"
        }
    }

    var glyph: String {
        switch phase {
        case .connected: return "waveform"
        case .connecting, .requesting: return "ellipsis"
        case .failed: return "exclamationmark.triangle"
        default: return "bolt.horizontal"
        }
    }

    var busy: Bool { phase == .requesting || phase == .connecting }

    var banner: String? {
        switch phase {
        case .connected:
            return earActive ? "Listening on this Mac" : "Connected — another Mac has the ear"
        case .idle:
            return paired ? "Ready to talk" : "Not paired yet"
        case .pairing:
            return "Waiting for the code"
        case .requesting:
            return "Asking Jarvis…"
        case .connecting:
            return "Connecting…"
        case .failed(let reason):
            return reason
        }
    }

    var updateAvailable: Bool {
        let remote = serverVersion.trimmingCharacters(in: .whitespaces)
        return !remote.isEmpty && remote != "—" && remote != "unreachable" && remote != appVersion
    }

    // MARK: - The one button

    func primaryAction() {
        switch phase {
        case .idle:
            if paired { connect() } else { requestCode() }
        case .requesting, .connecting:
            break
        case .pairing:
            pair()
        case .connected:
            disconnect()
        case .failed:
            phase = .idle
            detail = ""
            if paired { connect() } else { requestCode() }
        }
    }

    func requestCode() {
        guard phase != .requesting else { return }
        guard let server else {
            detail = "Set the server address in Settings first."
            return
        }
        phase = .requesting
        detail = "Asking Jarvis for a pairing code…"
        Task {
            do {
                let expires = try await JarvisApi(server: server).requestCode(name: hostName)
                self.detail = "The code is on `jarvis dashboard` on the Mac running Jarvis (\(max(1, expires / 60)) minutes)."
                self.phase = .pairing
            } catch {
                self.phase = .failed("Could not reach Jarvis")
                self.detail = error.localizedDescription
            }
        }
    }

    func pair() {
        guard !pairingInFlight else { return }
        guard let server else {
            detail = "Set the server address in Settings first."
            return
        }
        let code = codeInput.filter(\.isNumber)
        guard code.count == 6 else {
            detail = "The code is six digits, shown on `jarvis dashboard`."
            return
        }
        pairingInFlight = true
        detail = "Pairing…"
        Task {
            defer { self.pairingInFlight = false }
            do {
                let record = try await JarvisApi(server: server).pair(code: code, name: hostName)
                try RemoteFiles.saveCredentials(record)
                self.credentials = record
                self.paired = true
                self.deviceName = record.name
                self.serverURL = record.server
                self.codeInput = ""
                self.connect()
            } catch let error as ApiError where error.status == 403 || error.status == 429 {
                self.detail = "That code was wrong or expired. Ask for a new one on `jarvis dashboard`."
                self.phase = .pairing
            } catch {
                self.phase = .failed("Pairing failed")
                self.detail = error.localizedDescription
            }
        }
    }

    func connect() {
        guard let record = credentials else {
            requestCode()
            return
        }
        attempt = 0
        open(record)
    }

    func disconnect() {
        relay?.stop()
        relay = nil
        earActive = false
        phase = .idle
        detail = ""
    }

    /// Unpair: forget the secret on this Mac. `jarvis devices revoke <id>` is how the other side cuts it off.
    func forget() {
        disconnect()
        RemoteFiles.deleteCredentials()
        credentials = nil
        paired = false
        deviceName = ""
        serverVersion = "—"
    }

    func saveServer() {
        if let credentials, normalizeServer(credentials.server) != server {
            // A different Jarvis means a different secret; the old one is no use there.
            forget()
        }
        if let server = server {
            try? FileManager.default.createDirectory(at: RemoteFiles.dir, withIntermediateDirectories: true)
            try? (server + "\n").write(to: RemoteFiles.server, atomically: true, encoding: .utf8)
            serverURL = server
        }
        Task { await refresh() }
    }

    func refresh() async {
        guard let server else { return }
        do {
            serverVersion = try await JarvisApi(server: server).health().version
        } catch {
            serverVersion = "unreachable"
        }
    }

    // MARK: - The relay

    private func open(_ record: Credentials) {
        let relay = AudioRelay()
        self.relay = relay
        phase = .connecting
        detail = "Connecting to \(record.server)…"
        relay.onOpened = { [weak self] in
            Task { @MainActor in self?.opened() }
        }
        relay.onEar = { [weak self] active in
            Task { @MainActor in self?.earActive = active }
        }
        relay.onOutcome = { [weak self] outcome in
            Task { @MainActor in self?.outcome(outcome) }
        }
        let url = record.url.isEmpty ? "ws\(record.server.dropFirst(4))/ws" : record.url
        do {
            try relay.start(url: url, secret: record.secret)
        } catch {
            phase = .failed("Audio could not start")
            detail = error.localizedDescription
        }
    }

    private func opened() {
        phase = .connected
        detail = "Talk on this Mac. Ctrl+C is not needed; Disconnect hands the mic back."
        Task { await refresh() }
    }

    private func outcome(_ outcome: AudioRelay.Outcome) {
        relay = nil
        earActive = false
        switch outcome {
        case .stopped:
            phase = .idle
            detail = ""
        case .busy:
            phase = .failed("Another Mac is already connected")
            detail = "Jarvis has one ear. Disconnect the other Mac first."
        case .revoked:
            forget()
            phase = .idle
            message = "Jarvis revoked this Mac's pairing. Pair again with a new code."
        case .silentMic:
            phase = .failed("macOS is not sending any microphone audio")
            detail = "Allow Microphone for Jarvis in System Settings > Privacy & Security > Microphone, then connect again."
        case .dropped:
            retry()
        }
    }

    private func retry() {
        guard let record = credentials, let server = normalizeServer(record.server) else { return }
        guard attempt < 5 else {
            phase = .failed("Lost the connection to Jarvis")
            detail = "Is the Mac mini awake and on this network?"
            return
        }
        attempt += 1
        phase = .connecting
        detail = "Reconnecting… (\(attempt) of 5)"
        Task {
            // A refused secret is not a flaky network: it means this Mac was cut off, so pair again.
            if let works = await JarvisApi(server: server).secretWorks(record.secret), !works {
                self.forget()
                self.phase = .idle
                self.message = "This Mac's pairing no longer works. Pair again with a new code."
                return
            }
            try? await Task.sleep(nanoseconds: 2_000_000_000)
            // Disconnect during the wait wins: the user asked for it.
            guard self.phase == .connecting, self.credentials != nil else { return }
            self.open(record)
        }
    }

    // MARK: - Update

    /// Pull this checkout's app from the server and swap it in, then relaunch. The server built it; nothing is patched.
    func installUpdate() {
        guard let server, !updating else { return }
        updating = true
        detail = "Downloading the newer app from \(server)…"
        Task {
            do {
                try await self.applyUpdate(server: server)
                self.detail = "Restarting on the new version…"
                try? await Task.sleep(nanoseconds: 300_000_000)
                NSApplication.shared.terminate(nil)
            } catch {
                self.updating = false
                self.phase = .failed("The update did not install")
                self.detail = error.localizedDescription
            }
        }
    }

    private func applyUpdate(server: String) async throws {
        let fm = FileManager.default
        let work = fm.temporaryDirectory.appendingPathComponent("jarvis-update-\(UUID().uuidString)")
        try fm.createDirectory(at: work, withIntermediateDirectories: true)
        defer { try? fm.removeItem(at: work) }

        guard let url = URL(string: server + "/Jarvis.app.zip") else {
            throw ApiError(status: 0, message: "That server address is not a URL.")
        }
        let (download, response) = try await URLSession.shared.download(from: url)
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard (200..<300).contains(status) else {
            throw ApiError(status: status, message: "The server has no app to download (\(status)). Build it with `jarvis start`.")
        }
        try fm.moveItem(at: download, to: work.appendingPathComponent("Jarvis.app.zip"))
        try run("/usr/bin/ditto", ["-xk", work.appendingPathComponent("Jarvis.app.zip").path, work.path])

        let fresh = work.appendingPathComponent("Jarvis.app")
        guard fm.fileExists(atPath: fresh.path) else {
            throw ApiError(status: 0, message: "The download is not a Jarvis.app bundle.")
        }
        try run("/usr/bin/codesign", ["--verify", "--deep", "--strict", fresh.path])

        let bundle = Bundle.main.bundlePath
        let old = work.appendingPathComponent("Jarvis.old.app")
        if fm.fileExists(atPath: bundle) { try fm.moveItem(atPath: bundle, toPath: old.path) }
        do {
            try run("/usr/bin/ditto", [fresh.path, bundle])
        } catch {
            if fm.fileExists(atPath: old.path) { try? fm.moveItem(atPath: old.path, toPath: bundle) }
            throw ApiError(
                status: 0,
                message: "Could not write \(bundle). Install again with `curl -fsSL \(server)/install-app | sh`."
            )
        }
        _ = try? run("/usr/bin/xattr", ["-dr", "com.apple.quarantine", bundle])
        _ = try? run("/usr/bin/open", ["-n", bundle])
    }

    @discardableResult
    private func run(_ tool: String, _ arguments: [String]) throws -> String {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: tool)
        process.arguments = arguments
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        try process.run()
        let output = pipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        let text = String(data: output, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        guard process.terminationStatus == 0 else {
            throw ApiError(status: Int(process.terminationStatus), message: text.isEmpty ? "\(tool) failed." : text)
        }
        return text
    }

    private var hostName: String {
        let name = ProcessInfo.processInfo.hostName
        return name.split(separator: ".").first.map(String.init) ?? name
    }
}

import AVFoundation
import Foundation

/// This Mac's mic and speaker over the Jarvis relay, with the echo cancellation barge-in depends on.
///
/// The socket carries 16-bit mono PCM at 16 kHz both ways plus JSON control (`hello`, `flush`, `ear`), exactly like
/// the zero-install Python client; every model, key and agent stays on the Mac running Jarvis.
final class AudioRelay: NSObject, URLSessionWebSocketDelegate {
    enum Outcome: Equatable {
        case stopped        // we closed it
        case dropped        // the socket went away; worth retrying
        case busy           // another Mac holds the mic (1013)
        case revoked        // this Mac's pairing was revoked (1008)
        case silentMic      // connected, but macOS delivered no samples
    }

    static let sampleRate = 16_000.0
    /// Mic audio waiting for the socket, in blocks: past a second behind, the oldest goes rather than lag.
    private static let maxPendingSends = 50
    private static let silentMicSeconds = 3.0

    var onOpened: (() -> Void)?
    var onOutcome: ((Outcome) -> Void)?
    var onEar: ((Bool) -> Void)?

    private let engine = AVAudioEngine()
    private let player = AVAudioPlayerNode()
    private let lock = NSLock()
    private var converter: AVAudioConverter?
    private var wire: AVAudioFormat?
    private var session: URLSession?
    private var task: URLSessionWebSocketTask?
    private var pendingSends = 0
    private var micBytes = 0
    private var silenceTimer: DispatchSourceTimer?
    private var running = false
    private var finished = false

    // MARK: - Lifecycle

    func start(url: String, secret: String) throws {
        stop()
        guard let endpoint = URL(string: url) else {
            throw ApiError(status: 0, message: "This Mac's saved server is not a URL.")
        }
        try startAudio()
        var request = URLRequest(url: endpoint)
        request.setValue("Bearer \(secret)", forHTTPHeaderField: "Authorization")
        let session = URLSession(configuration: .default, delegate: self, delegateQueue: nil)
        let task = session.webSocketTask(with: request)
        self.session = session
        self.task = task
        running = true
        finished = false
        micBytes = 0
        task.resume()
        receive()
    }

    func stop() {
        guard running || task != nil else { return }
        running = false
        stopAudio()
        silenceTimer?.cancel()
        silenceTimer = nil
        task?.cancel(with: .normalClosure, reason: nil)
        task = nil
        session?.invalidateAndCancel()
        session = nil
    }

    // MARK: - Audio

    private func startAudio() throws {
        guard let wire = AVAudioFormat(
            commonFormat: .pcmFormatInt16, sampleRate: Self.sampleRate, channels: 1, interleaved: true
        ) else { throw ApiError(status: 0, message: "No 16 kHz mono audio format on this Mac.") }
        self.wire = wire

        let input = engine.inputNode
        _ = engine.mainMixerNode  // the output side must exist before voice processing turns on
        _ = engine.outputNode
        if !input.isVoiceProcessingEnabled {
            try input.setVoiceProcessingEnabled(true)
        }
        // Voice processing otherwise turns every other app's sound down; keep that to the least macOS allows.
        if #available(macOS 14.0, *) {
            input.voiceProcessingOtherAudioDuckingConfiguration =
                .init(enableAdvancedDucking: false, duckingLevel: .min)
        }

        let micFormat = input.outputFormat(forBus: 0)
        guard micFormat.sampleRate > 0, let converter = AVAudioConverter(from: micFormat, to: wire) else {
            throw ApiError(status: 0, message: "macOS voice processing could not start: no usable mic format.")
        }
        self.converter = converter

        engine.attach(player)
        engine.connect(player, to: engine.mainMixerNode, format: wire)
        input.installTap(onBus: 0, bufferSize: AVAudioFrameCount(micFormat.sampleRate / 50), format: micFormat) {
            [weak self] buffer, _ in
            self?.sendMic(buffer)
        }
        engine.prepare()
        try engine.start()
        player.play()
        prime()
    }

    private func stopAudio() {
        if engine.isRunning {
            engine.inputNode.removeTap(onBus: 0)
            player.stop()
            engine.stop()
        }
        if engine.attachedNodes.contains(player) {
            engine.detach(player)
        }
        converter = nil
    }

    /// Voice processing sends nothing until the output has played a reference signal: half a second of silence.
    private func prime() {
        guard let wire, let buffer = AVAudioPCMBuffer(pcmFormat: wire, frameCapacity: AVAudioFrameCount(Self.sampleRate / 2))
        else { return }
        buffer.frameLength = AVAudioFrameCount(Self.sampleRate / 2)
        if let samples = buffer.int16ChannelData {
            memset(samples[0], 0, Int(buffer.frameLength) * MemoryLayout<Int16>.size)
        }
        player.scheduleBuffer(buffer)
        player.play()
    }

    private func sendMic(_ buffer: AVAudioPCMBuffer) {
        guard running, let converter, let wire else { return }
        let ratio = wire.sampleRate / buffer.format.sampleRate
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 64
        guard let converted = AVAudioPCMBuffer(pcmFormat: wire, frameCapacity: capacity) else { return }
        var supplied = false
        var conversionError: NSError?
        let status = converter.convert(to: converted, error: &conversionError) { _, outStatus in
            if supplied {
                outStatus.pointee = .noDataNow
                return nil
            }
            supplied = true
            outStatus.pointee = .haveData
            return buffer
        }
        guard status != .error, converted.frameLength > 0, let samples = converted.int16ChannelData else { return }
        let data = Data(bytes: samples[0], count: Int(converted.frameLength) * MemoryLayout<Int16>.size)
        lock.lock()
        micBytes += data.count
        lock.unlock()
        send(data)
    }

    private func play(_ data: Data) {
        guard let wire, !data.isEmpty else { return }
        let frames = AVAudioFrameCount(data.count / MemoryLayout<Int16>.size)
        guard let buffer = AVAudioPCMBuffer(pcmFormat: wire, frameCapacity: frames),
              let samples = buffer.int16ChannelData
        else { return }
        buffer.frameLength = frames
        data.withUnsafeBytes { raw in
            if let base = raw.baseAddress { memcpy(samples[0], base, data.count) }
        }
        player.scheduleBuffer(buffer)
    }

    /// An interruption: drop what is queued but not yet played.
    private func flush() {
        player.stop()
        player.play()
    }

    // MARK: - Socket

    private func send(_ data: Data) {
        lock.lock()
        guard pendingSends < Self.maxPendingSends else {
            lock.unlock()
            return  // the socket is behind: drop the block rather than lag behind the speaker
        }
        pendingSends += 1
        lock.unlock()
        task?.send(.data(data)) { [weak self] _ in
            guard let self else { return }
            self.lock.lock()
            self.pendingSends -= 1
            self.lock.unlock()
        }
    }

    private func receive() {
        task?.receive { [weak self] result in
            guard let self else { return }
            switch result {
            case .failure:
                self.finish(.dropped)
            case .success(let message):
                switch message {
                case .data(let data): self.play(data)
                case .string(let text): self.control(text)
                @unknown default: break
                }
                self.receive()
            }
        }
    }

    private func control(_ text: String) {
        guard let control = try? JSONSerialization.jsonObject(with: Data(text.utf8)) as? [String: Any],
              let kind = control["type"] as? String
        else { return }
        switch kind {
        case "flush": flush()
        case "hello": opened()
        case "ear": onEar?(control["active"] as? Bool ?? false)
        default: break
        }
    }

    private func opened() {
        guard !finished else { return }
        onOpened?()
        let timer = DispatchSource.makeTimerSource(queue: .main)
        timer.schedule(deadline: .now() + Self.silentMicSeconds)
        timer.setEventHandler { [weak self] in
            guard let self else { return }
            self.lock.lock()
            let heard = self.micBytes
            self.lock.unlock()
            if heard == 0 { self.finish(.silentMic) }
        }
        silenceTimer?.cancel()
        silenceTimer = timer
        timer.resume()
    }

    private func finish(_ outcome: Outcome) {
        guard !finished else { return }
        finished = true
        running = false
        silenceTimer?.cancel()
        silenceTimer = nil
        stopAudio()
        task?.cancel(with: .normalClosure, reason: nil)
        task = nil
        session?.invalidateAndCancel()
        session = nil
        onOutcome?(outcome)
    }

    // MARK: - URLSessionWebSocketDelegate

    func urlSession(
        _ session: URLSession, webSocketTask: URLSessionWebSocketTask,
        didCloseWith closeCode: URLSessionWebSocketTask.CloseCode, reason: Data?
    ) {
        switch closeCode {
        case .policyViolation:
            finish(.revoked)  // 1008: `jarvis devices revoke`, or the server was told to cut this Mac off
        case URLSessionWebSocketTask.CloseCode(rawValue: 1013):
            finish(.busy)  // another Mac already holds the one ear
        default:
            finish(.dropped)
        }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        if error != nil { finish(.dropped) }
    }
}

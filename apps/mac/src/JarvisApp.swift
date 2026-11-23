import AppKit
import SwiftUI

@main
struct JarvisApp: App {
    @StateObject private var model = AppModel()

    var body: some Scene {
        WindowGroup("Jarvis") {
            ContentView()
                .environmentObject(model)
                .frame(width: 380)
        }
        .windowResizability(.contentSize)
        .commands {
            CommandGroup(replacing: .newItem) {}
        }
    }
}

/// The whole app: one big button, and Settings behind the gear.
struct ContentView: View {
    @EnvironmentObject private var model: AppModel
    @State private var showsSettings = false

    var body: some View {
        VStack(spacing: 20) {
            header
            connectButton
            status
            if model.phase == .pairing { codeField }
            Spacer(minLength: 0)
            footer
        }
        .padding(.horizontal, 28)
        .padding(.vertical, 22)
        .background(Color(nsColor: .windowBackgroundColor))
        .sheet(isPresented: $showsSettings) {
            SettingsView(isPresented: $showsSettings)
                .environmentObject(model)
        }
        .alert(
            "Jarvis",
            isPresented: Binding(
                get: { model.message != nil },
                set: { if !$0 { model.message = nil } }
            )
        ) {
            Button("OK", role: .cancel) { model.message = nil }
        } message: {
            Text(model.message ?? "")
        }
    }

    private var header: some View {
        HStack {
            Text("Jarvis")
                .font(.system(size: 20, weight: .semibold, design: .rounded))
            Spacer()
            Button {
                showsSettings = true
            } label: {
                Image(systemName: "gearshape")
                    .font(.system(size: 16, weight: .medium))
                    .foregroundStyle(.secondary)
            }
            .buttonStyle(.plain)
            .help("Settings")
        }
    }

    private var accent: Color {
        switch model.phase {
        case .connected: return .green
        case .failed: return .orange
        case .connecting, .requesting: return .gray
        default: return .accentColor
        }
    }

    private var connectButton: some View {
        Button(action: { model.primaryAction() }) {
            ZStack {
                Circle()
                    .fill(accent.opacity(0.14))
                    .frame(width: 168, height: 168)
                Circle()
                    .fill(accent)
                    .frame(width: 132, height: 132)
                    .shadow(color: accent.opacity(0.35), radius: 14, y: 6)
                VStack(spacing: 8) {
                    Image(systemName: model.glyph)
                        .font(.system(size: 38, weight: .semibold))
                    Text(model.buttonLabel)
                        .font(.system(size: 15, weight: .semibold))
                }
                .foregroundStyle(.white)
            }
        }
        .buttonStyle(.plain)
        .disabled(model.busy)
        .accessibilityLabel(model.buttonLabel)
    }

    private var status: some View {
        VStack(spacing: 6) {
            if let banner = model.banner {
                Text(banner)
                    .font(.system(size: 14, weight: .medium))
                    .multilineTextAlignment(.center)
            }
            if !model.detail.isEmpty {
                Text(model.detail)
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: .infinity)
    }

    private var codeField: some View {
        VStack(spacing: 8) {
            TextField("123456", text: $model.codeInput)
                .textFieldStyle(.plain)
                .font(.system(size: 28, weight: .semibold, design: .monospaced))
                .multilineTextAlignment(.center)
                .onSubmit { model.primaryAction() }
                .padding(.vertical, 8)
                .background(
                    RoundedRectangle(cornerRadius: 10)
                        .fill(Color(nsColor: .textBackgroundColor))
                        .overlay(RoundedRectangle(cornerRadius: 10).stroke(Color.secondary.opacity(0.3)))
                )
            Text("Six digits, from `jarvis top` on the Mac mini.")
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
        }
        .onAppear { model.codeInput = "" }
    }

    private var footer: some View {
        VStack(spacing: 2) {
            Text(model.paired ? model.deviceName : "Not paired with a Jarvis yet")
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(.secondary)
            Text(model.serverURL.isEmpty ? "no server set" : model.serverURL)
                .font(.system(size: 11, design: .monospaced))
                .foregroundStyle(.tertiary)
                .lineLimit(1)
                .truncationMode(.middle)
        }
    }
}

/// Version on both sides, the update button, the server address, and unpair.
struct SettingsView: View {
    @EnvironmentObject private var model: AppModel
    @Binding var isPresented: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Settings")
                .font(.system(size: 16, weight: .semibold))

            Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 8) {
                GridRow {
                    Text("App").foregroundStyle(.secondary)
                    Text(model.appVersion).font(.system(size: 12, design: .monospaced))
                }
                GridRow {
                    Text("Server").foregroundStyle(.secondary)
                    Text(model.serverVersion).font(.system(size: 12, design: .monospaced))
                }
            }

            HStack(spacing: 10) {
                Button(model.updating ? "Updating…" : "Update") { model.installUpdate() }
                    .disabled(!model.updateAvailable || model.updating)
                Button("Check again") { Task { await model.refresh() } }
                if model.updateAvailable && !model.updating {
                    Text("a newer build is on the server")
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                }
            }

            Divider()

            VStack(alignment: .leading, spacing: 6) {
                Text("Jarvis server")
                    .font(.system(size: 12, weight: .medium))
                TextField("mac-mini.local:8643", text: $model.serverURL)
                    .textFieldStyle(.roundedBorder)
                    .font(.system(size: 12, design: .monospaced))
                Text("A name on your network, or a Tailscale address.")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                HStack {
                    Button("Save") { model.saveServer() }
                        .disabled(normalizeServer(model.serverURL) == nil)
                    Button("Unpair this Mac", role: .destructive) { model.forget() }
                }
            }

            Spacer(minLength: 0)

            HStack {
                Spacer()
                Button("Done") { isPresented = false }
                    .keyboardShortcut(.defaultAction)
            }
        }
        .padding(22)
        .frame(width: 400, height: 340)
    }
}

//! Pane 1 — how one message travels: ear, voice checks, the executive model, the gateway, and the brain.
//!
//! Five columns left to right, wired with arrows that light up when the stage they lead into is working. Every
//! node is selectable; `Enter` opens the same "x-ray" text the architecture modal has always shown, now with the
//! live numbers for the turn on screen.

use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::Style;
use ratatui::text::{Line, Span, Text};
use ratatui::widgets::Paragraph;
use ratatui::Frame;

use crate::app::{App, Pane};
use crate::ui::grid::wrap_text;
use crate::ui::theme;

pub struct NodeSpec {
    pub id: &'static str,
    pub title: &'static str,
    pub summary: &'static str,
    pub detail: &'static str,
    pub file: &'static str,
}

/// Column by column, in the order a sentence moves through Jarvis.
pub const NODES: [NodeSpec; 15] = [
    NodeSpec {
        id: "local",
        title: "Local Mac",
        summary: "AVAudioEngine mic + speaker",
        detail: "One engine with macOS voice processing owns the mic and the speaker, so Jarvis cancels its own voice and you can talk over it. With no local hardware the ear waits for a paired Mac.",
        file: "apps/voice/src/jarvis_voice/mac_audio.py",
    },
    NodeSpec {
        id: "remote",
        title: "Paired Mac",
        summary: "/ws · 16 kHz PCM",
        detail: "A paired Mac's mic and speaker ride the same gateway; the client runs the echo canceller where the speaker is. The ear follows whichever Mac hears you, one client at a time.",
        file: "apps/voice/src/jarvis_voice/remote_audio.py",
    },
    NodeSpec {
        id: "meet",
        title: "Meet / CLI",
        summary: "Chrome tap · /v1/ask",
        detail: "Meeting mode hears the call through the system audio tap and speaks back through a Chrome extension. The CLI, the cockpit and an iPhone Shortcut are ordinary authenticated HTTP calls.",
        file: "apps/voice/src/jarvis_voice/meet_voice.py",
    },
    NodeSpec {
        id: "vad",
        title: "VAD + Turn",
        summary: "Silero VAD · Smart Turn v3",
        detail: "Silero finds speech in under a millisecond; Smart Turn v3 (local ONNX) decides whether a pause ends the sentence, holding mid-thought up to 1.5 s so Whisper gets it whole.",
        file: "apps/voice/src/jarvis_voice/voice_gates.py",
    },
    NodeSpec {
        id: "stt",
        title: "Whisper + Spk",
        summary: "MLX Whisper · master check",
        detail: "Whisper runs locally on the Apple GPU while a sherpa-onnx speaker embedding is scored against the enrolled voiceprint; a sentence below SPEAKER_THRESHOLD is dropped before it can start a turn.",
        file: "apps/voice/src/jarvis_voice/voice_gates.py",
    },
    NodeSpec {
        id: "intent",
        title: "Intent + Clean",
        summary: "Laya jarvis-intent · cleanup",
        detail: "With no wake word, Laya's fine-tuned intent model (FILTER_MODEL as its fallback) decides whether the sentence was meant for Jarvis. Mute, unmute and meeting mode settle in code, with no model call.",
        file: "apps/voice/src/jarvis_voice/executive_filter.py",
    },
    NodeSpec {
        id: "mem0",
        title: "Mem0 + Ctx",
        summary: "Qdrant · bge-m3",
        detail: "Mem0 searches local Qdrant with bge-m3 embeddings (~22 ms) and injects what it found right before the newest user message, so prompt caching stays warm. Guests and room talk get no memories.",
        file: "apps/voice/src/jarvis_voice/memory.py",
    },
    NodeSpec {
        id: "exec",
        title: "Exec LLM",
        summary: "OpenRouter · thinking on",
        detail: "The conversation model reads the prepared context and calls one tool: send_agent_task, answer_agent or stop_agent. Its reasoning stream and each tool call are teed to this cockpit.",
        file: "apps/voice/src/jarvis_voice/llm.py",
    },
    NodeSpec {
        id: "tts",
        title: "State + TTS",
        summary: "VOICE / BEEP_ONLY · ElevenLabs",
        detail: "VOICE mode speaks through ElevenLabs eleven_v4_turbo. 'Shut up' switches to BEEP_ONLY: playback stops, agent topics queue, and a chime marks each one until 'you can talk'.",
        file: "apps/voice/src/jarvis_voice/state.py",
    },
    NodeSpec {
        id: "gw",
        title: "BrainServer",
        summary: "aiohttp · SSE · auth",
        detail: "One aiohttp app on :8643 serves the brain's run API, the board, timers, the outbox, telemetry, the paired Mac's audio and the mac app. Every route but /health needs a token.",
        file: "apps/brain/src/brain/server.py",
    },
    NodeSpec {
        id: "route",
        title: "Router",
        summary: "one cheap tool call",
        detail: "BrainOrchestrator turns one request into one runner call: delegate_project, ask_ephemeral_pi, list_sessions, queue_board_task or reply_direct, with a local rule when the router is unavailable.",
        file: "apps/brain/src/brain/orchestrator.py",
    },
    NodeSpec {
        id: "runners",
        title: "T3 / Pi",
        summary: "threads · one-shot pi",
        detail: "T3Runner keeps one WebSocket to t3 serve, binds a repo to a thread and relays approvals verbatim; EphemeralPiRunner answers small questions in a 0700 temp dir. Claude and OpenCode run only when named.",
        file: "apps/brain/src/brain/runners/",
    },
    NodeSpec {
        id: "db",
        title: "state.db",
        summary: "SQLite WAL · 7 tables",
        detail: "Single writer: tasks, attempts, threads, skills, outbox, timers and system_state. This cockpit reads the same file on a read-only connection, so it never blocks the daemon.",
        file: "apps/brain/src/brain/db.py",
    },
    NodeSpec {
        id: "rec",
        title: "Reconciler",
        summary: "board · pi builders",
        detail: "The tick loop claims ready board tasks in the mode that matches, runs a sandboxed pi builder in a worktree on its own branch, verifies, and pushes — never merging to master on its own.",
        file: "apps/brain/src/brain/reconciler.py",
    },
    NodeSpec {
        id: "outbox",
        title: "Improver / Out",
        summary: "reflection · outbox",
        detail: "The offline Improver curates memory and skills from attempt traces. Queued outbox items are spoken in ASSIST mode and stay quiet in AUTONOMOUS until you ask for the brief.",
        file: "apps/brain/src/brain/memory.py",
    },
];

/// Which column each node sits in, and the header above it.
const COLUMNS: [(&str, [usize; 3]); 5] = [
    ("1 EAR/IO", [0, 1, 2]),
    ("2 VOICE IN", [3, 4, 5]),
    ("3 EXEC/OUT", [6, 7, 8]),
    ("4 GATEWAY", [9, 10, 11]),
    ("5 BRAIN", [12, 13, 14]),
];

pub fn node_spec(index: usize) -> &'static NodeSpec {
    &NODES[index.min(NODES.len() - 1)]
}

pub fn node_text(index: usize) -> String {
    let spec = node_spec(index);
    format!("{}\n{}\n\n{}\n\n{}", spec.title, spec.summary, spec.detail, spec.file)
}

pub fn draw(frame: &mut Frame, app: &App, area: Rect) {
    let focused = app.focus == Pane::Topology;
    let title = format!(
        "1 LIVE FLOW · {} · enter = inspect · green working · amber return",
        app.ear
    );
    let block = theme::panel(focused, &title);
    let inner = block.inner(area);
    frame.render_widget(block, area);
    if inner.width < 36 || inner.height < 8 {
        return;
    }

    let mut constraints = Vec::new();
    for column in 0..COLUMNS.len() {
        if column > 0 {
            constraints.push(Constraint::Length(1));
        }
        constraints.push(Constraint::Fill(1));
    }
    let chunks = Layout::horizontal(constraints).split(inner);

    for (column, (header, nodes)) in COLUMNS.iter().enumerate() {
        let rect = chunks[column * 2];
        let text = column_text(app, header, nodes, rect.width as usize);
        frame.render_widget(Paragraph::new(text).style(theme::base()), rect);
    }

    // The wire between two columns: ▶ when the column it feeds is working, ◀ for the way back.
    for column in 1..COLUMNS.len() {
        let rect = chunks[column * 2 - 1];
        let feeding = COLUMNS[column].1.iter().any(|idx| app.node_active(NODES[*idx].id));
        let returning = COLUMNS[column - 1]
            .1
            .iter()
            .any(|idx| NODES[*idx].id == "tts" && app.node_active("tts"));
        let style = if feeding {
            theme::bold(theme::ACCENT)
        } else {
            theme::faint()
        };
        let back = if returning {
            theme::bold(theme::WARN)
        } else {
            theme::faint()
        };
        let lines = vec![
            Line::from(""),
            Line::from(""),
            Line::from(Span::styled("▶", style)),
            Line::from(Span::styled("◀", back)),
        ];
        frame.render_widget(Paragraph::new(Text::from(lines)).style(theme::base()), rect);
    }

    // The return path, spelled out: an agent's markdown never reaches the speaker as it is.
    if inner.height >= 4 {
        let strip = Rect {
            x: inner.x,
            y: inner.y + inner.height - 1,
            width: inner.width,
            height: 1,
        };
        let newest = app.newest_trace();
        let returned = newest.and_then(|t| t.stage("RETURN")).map(|s| s.detail.clone());
        let spoken = newest.and_then(|t| t.spoken()).map(|s| s.text.clone());
        let mut spans = vec![Span::styled(
            "RETURN ↩ agent output → SSE → AGENT_REPORT_PROMPT → retell → TTS",
            theme::fg(theme::WARN),
        )];
        if let Some(detail) = returned {
            spans.push(Span::styled(format!("   last: {detail}"), theme::faint()));
        }
        if let Some(spoken) = spoken {
            spans.push(Span::styled(
                format!("   spoke: {}", theme::clip_ellipsis(&spoken, 70)),
                theme::fg(theme::TEXT),
            ));
        }
        frame.render_widget(Paragraph::new(Line::from(spans)).style(theme::base()), strip);
    }
}

fn column_text(app: &App, header: &str, nodes: &[usize; 3], width: usize) -> Text<'static> {
    // Two lines per node, clipped to the column: a long subtitle must not push the nodes below it out of line.
    let room = width.saturating_sub(2).max(6);
    let mut lines = vec![Line::from(Span::styled(
        theme::clip_ellipsis(header, width),
        theme::bold(theme::PURPLE),
    ))];
    for index in nodes {
        let spec = node_spec(*index);
        let active = app.node_active(spec.id);
        let selected = app.sel_topology == *index && app.focus == Pane::Topology;
        let marker = if selected { "▌" } else { " " };
        let style = if selected {
            theme::bold(theme::CYAN)
        } else if active {
            theme::bold(theme::TEXT)
        } else {
            theme::fg(theme::TEXT)
        };
        let dot = if active {
            Span::styled("●", theme::fg(theme::ACCENT))
        } else {
            Span::styled("○", theme::faint())
        };
        lines.push(Line::from(vec![
            Span::styled(format!("{marker}{}", theme::clip_ellipsis(spec.title, room)), style),
            Span::raw(" "),
            dot,
        ]));
        let sub = live_subtitle(app, spec.id);
        let sub_style = if active {
            theme::fg(theme::ACCENT)
        } else {
            theme::dim()
        };
        lines.push(Line::from(Span::styled(
            format!("  {}", theme::clip_ellipsis(&sub, room)),
            sub_style,
        )));
    }
    Text::from(lines)
}

/// The live number beside a node when there is one, else the plain description of what it is.
fn live_subtitle(app: &App, id: &str) -> String {
    let newest = app.newest_trace();
    match id {
        "local" | "remote" => app.ear.clone(),
        "stt" => newest
            .and_then(|t| t.stage("STT"))
            .map(|s| s.detail.clone())
            .unwrap_or_else(|| "Whisper + voiceprint".to_string()),
        "intent" => newest
            .and_then(|t| t.stage("INTENT"))
            .map(|s| format!("{} · {}", s.text, s.detail))
            .unwrap_or_else(|| "Laya + cleanup".to_string()),
        "mem0" => newest
            .and_then(|t| t.stage("MEM"))
            .map(|s| s.detail.clone())
            .unwrap_or_else(|| "Qdrant · bge-m3".to_string()),
        "exec" => newest
            .and_then(|t| t.stage("PREP"))
            .map(|s| s.detail.clone())
            .unwrap_or_else(|| "thinking on".to_string()),
        "tts" => app.voice_mode.clone(),
        "gw" => format!(":{} · {}", app.cfg.port, if app.daemon_connected { "live" } else { "offline" }),
        "route" => newest
            .and_then(|t| t.stage("ROUTE"))
            .map(|s| format!("{} {}", s.text, s.detail))
            .unwrap_or_else(|| "one tool call".to_string()),
        "runners" => newest
            .and_then(|t| t.stage("DISPATCH"))
            .map(|s| s.detail.clone())
            .unwrap_or_else(|| "threads · pi".to_string()),
        "db" => format!("{} tasks · {} attempts", app.snapshot.counts.tasks, app.snapshot.counts.attempts),
        "rec" => short_phase(&app.phase),
        "outbox" => format!("{} queued", app.snapshot.counts.outbox_queued),
        "vad" => "Silero · Smart Turn".to_string(),
        "meet" => "tap · /v1/ask".to_string(),
        _ => String::new(),
    }
}

pub fn short_phase(phase: &str) -> String {
    match phase {
        "assist_idle" => "assist".to_string(),
        "budget_paused" => "budget".to_string(),
        "pi_running" => "pi running".to_string(),
        "pi_finished" => "done".to_string(),
        "skill_improver" => "reflect".to_string(),
        "auto_research" => "research".to_string(),
        other => theme::clip(other, 12),
    }
}

/// Renders the "Architecture X-Ray" modal body for the selected node.
pub fn node_modal_lines(app: &App, index: usize) -> Vec<ratatui::text::Line<'static>> {
    fn fact(lines: &mut Vec<ratatui::text::Line<'static>>, label: &str, value: String, style: Style) {
        lines.push(ratatui::text::Line::from(vec![
            Span::styled(format!("{label:<16}"), theme::dim()),
            Span::styled(value, style),
        ]));
    }

    let spec = node_spec(index);
    let mut lines = vec![
        ratatui::text::Line::from(Span::styled(spec.summary.to_string(), theme::bold(theme::ACCENT))),
        ratatui::text::Line::from(Span::styled(spec.file.to_string(), theme::faint())),
        ratatui::text::Line::from(""),
    ];
    for chunk in wrap_text(spec.detail, 74) {
        lines.push(ratatui::text::Line::from(Span::styled(chunk, theme::fg(theme::TEXT))));
    }
    lines.push(ratatui::text::Line::from(""));

    fact(
        &mut lines,
        "active",
        if app.node_active(spec.id) { "yes".into() } else { "no".into() },
        if app.node_active(spec.id) { theme::fg(theme::ACCENT) } else { theme::dim() },
    );
    fact(&mut lines, "live", live_subtitle(app, spec.id), theme::fg(theme::CYAN));

    // The turn on screen, stage by stage, so the modal explains the numbers next to the node.
    if let Some(trace) = app.newest_trace() {
        lines.push(ratatui::text::Line::from(""));
        lines.push(ratatui::text::Line::from(Span::styled(
            format!("LAST TURN #{}", trace.id),
            theme::bold(theme::PURPLE),
        )));
        for stage in trace.stages.iter().rev().take(6).collect::<Vec<_>>().into_iter().rev() {
            lines.push(ratatui::text::Line::from(vec![
                Span::styled(format!("{:<9}", stage.kind), theme::fg(theme::BLUE)),
                Span::styled(theme::clip_ellipsis(&stage.text, 40), theme::fg(theme::TEXT)),
                Span::styled(format!("  {}", stage.detail), theme::faint()),
            ]));
        }
    }
    lines
}

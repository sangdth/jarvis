//! Pane 4 — the live firehose: every telemetry event the daemon publishes, filtered.
//!
//! `v` keeps the voice's turn, `b` the brain's routing and runs, `e` only what went wrong, `A` everything. The
//! tail of the daemon log sits under it, so a crash or a worker's stderr is on the same screen as the events.

use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::Style;
use ratatui::text::{Line, Span, Text};
use ratatui::widgets::Paragraph;
use ratatui::Frame;

use crate::app::{App, Pane};
use crate::model::TelemetryEvent;
use crate::ui::theme;

pub fn draw(frame: &mut Frame, app: &mut App, area: Rect) {
    let focused = app.focus == Pane::Logs;
    let shown = app.events.iter().filter(|ev| app.log_filter.matches(ev)).count();
    let title = format!(
        "4 LOGS · {} · {shown}/{} lines",
        app.log_filter.label(),
        app.events.len()
    );
    let block = theme::panel(focused, &title);
    let inner = block.inner(area);
    frame.render_widget(block, area);
    if inner.width < 10 || inner.height < 3 {
        return;
    }

    // Two-thirds events, one-third the daemon log: the log explains what an event cannot.
    let parts = Layout::vertical([Constraint::Percentage(70), Constraint::Min(3)]).split(inner);
    frame.render_widget(Paragraph::new(event_lines(app, inner.width as usize)).style(theme::base()), parts[0]);

    let mut log_lines = vec![Line::from(Span::styled("DAEMON LOG · jarvis.err.log", theme::bold(theme::PURPLE)))];
    let tail = app.daemon_log();
    if tail.trim().is_empty() {
        log_lines.push(Line::from(Span::styled("  (empty or missing)", theme::dim())));
    } else {
        let take = parts[1].height.saturating_sub(2) as usize;
        for raw in tail.lines().rev().take(take).collect::<Vec<_>>().into_iter().rev() {
            let style = if raw.contains("ERROR") || raw.contains("Traceback") {
                theme::fg(theme::DANGER)
            } else if raw.contains("WARNING") {
                theme::fg(theme::WARN)
            } else {
                theme::dim()
            };
            log_lines.push(Line::from(Span::styled(
                theme::clip_ellipsis(raw, inner.width.saturating_sub(2) as usize),
                style,
            )));
        }
    }
    frame.render_widget(Paragraph::new(Text::from(log_lines)).style(theme::base()), parts[1]);
}

fn event_lines(app: &App, width: usize) -> Text<'static> {
    let matching: Vec<&TelemetryEvent> = app.events.iter().filter(|ev| app.log_filter.matches(ev)).collect();
    if matching.is_empty() {
        return Text::from(Line::from(Span::styled(
            "  no events yet (a standalone `jarvis` publishes none)",
            theme::dim(),
        )));
    }
    // Newest last, like a log; the visible window ends at the bottom.
    let mut lines: Vec<Line<'static>> = Vec::with_capacity(matching.len());
    for ev in matching {
        let (label, style) = kind_style(&ev.event);
        let mut spans = vec![
            Span::styled(format!("{:>6} ", ev.seq), theme::faint()),
            Span::styled(format!("{label:<8}"), style),
        ];
        let main = primary_field(ev);
        if !main.is_empty() {
            spans.push(Span::styled(
                theme::clip_ellipsis(&main, width.saturating_sub(20)),
                theme::fg(theme::TEXT),
            ));
        }
        lines.push(Line::from(spans));
    }
    Text::from(lines)
}

/// The one field a reader wants on the left of an event; the rest is noise until it is inspected.
fn primary_field(ev: &TelemetryEvent) -> String {
    for key in [
        "text",
        "user_tail",
        "intent",
        "choice",
        "summary",
        "output_preview",
        "input_preview",
        "detail",
        "error",
        "phase",
        "status",
    ] {
        let value = ev.text(key);
        if !value.is_empty() {
            let tool = ev.text("tool");
            if !tool.is_empty() {
                return format!("{tool} · {value}");
            }
            return value;
        }
    }
    String::new()
}

fn kind_style(event: &str) -> (String, Style) {
    let (label, style) = match event {
        "voice.stt" => ("STT", theme::fg(theme::CYAN)),
        "voice.intent" => ("INTENT", theme::fg(theme::ACCENT)),
        "voice.fastcmd" => ("FAST", theme::fg(theme::WARN)),
        "voice.cleanup" => ("CLEAN", theme::fg(theme::BLUE)),
        "voice.memory" => ("MEM", theme::fg(theme::PURPLE)),
        "voice.request" => ("PREP", theme::fg(theme::BLUE)),
        "voice.thinking" => ("THINK", theme::fg(theme::PURPLE)),
        "voice.tool" => ("TOOL", theme::fg(theme::CYAN)),
        "voice.latency" => ("LAT", theme::faint()),
        "voice.dispatch" => ("SEND", theme::fg(theme::ACCENT)),
        "voice.return" => ("RET", theme::fg(theme::WARN)),
        "voice.retell" => ("RETELL", theme::fg(theme::WARN)),
        "voice.tts" => ("TTS", theme::fg(theme::ACCENT)),
        "voice.mode" => ("MODE", theme::fg(theme::WARN)),
        "voice.ear" => ("EAR", theme::fg(theme::CYAN)),
        "brain.route" => ("ROUTE", theme::fg(theme::ACCENT)),
        "http.inbound" => ("HTTP", theme::fg(theme::WARN)),
        "http.completed" => ("DONE", theme::dim()),
        "run.status" => ("RUN", theme::fg(theme::CYAN)),
        "approval.request" => ("ASK", theme::bold(theme::WARN)),
        "approval.responded" => ("ANSWER", theme::fg(theme::ACCENT)),
        "reconciler.phase" => ("PHASE", theme::fg(theme::PURPLE)),
        "pi.progress" => ("PI", theme::fg(theme::BLUE)),
        "attempt.finished" => ("ATTEMPT", theme::fg(theme::CYAN)),
        "board.rpc" => ("BOARD", theme::fg(theme::PURPLE)),
        "pair.requested" => ("PAIR", theme::bold(theme::WARN)),
        "pair.resolved" => ("PAIRED", theme::fg(theme::ACCENT)),
        "heartbeat" => ("BEAT", theme::faint()),
        _ => return (theme::clip(event, 8), theme::dim()),
    };
    (label.to_string(), style)
}

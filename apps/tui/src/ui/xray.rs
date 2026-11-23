//! Pane 5 — full-height right sidebar combining Turn X-Ray, filtered event stream, and daemon log.
//!
//! One turn's stages (`STT` -> `INTENT` -> `CLEAN` -> `MEM` -> `PREP` -> `THINK` -> `TOOL` -> `TTS`) and the
//! broader telemetry firehose come from the same SSE bus; keeping them in one tall sidebar avoids painting the
//! same turn twice while keeping cross-turn events and `jarvis.err.log` in view at all times.

use ratatui::layout::{Alignment, Constraint, Layout, Rect};
use ratatui::style::Style;
use ratatui::text::{Line, Span, Text};
use ratatui::widgets::Paragraph;
use ratatui::Frame;

use crate::app::{App, LogFilter, Pane};
use crate::model::{StageTone, TelemetryEvent, Trace, TraceStage};
use crate::ui::grid::wrap_text;
use crate::ui::{mic_meter_line, theme};

pub fn draw(frame: &mut Frame, app: &mut App, area: Rect) {
    let focused = app.focus == Pane::Xray;
    let title = match app.selected_trace() {
        Some(trace) => format!("5 X-RAY · #{} · {}", trace.id, app.log_filter.label()),
        None => format!("5 X-RAY · {}", app.log_filter.label()),
    };
    let block = theme::panel(focused, &title);
    let inner = block.inner(area);
    frame.render_widget(block, area);

    // When anchored as the full-height right sidebar (`y == 0`), place the live mic meter at the top-right corner.
    if area.y == 0 && area.width >= 38 {
        let meter_w = 24u16.min(area.width.saturating_sub(2));
        let meter_rect = Rect {
            x: area.x + area.width.saturating_sub(meter_w + 1),
            y: area.y,
            width: meter_w,
            height: 1,
        };
        frame.render_widget(
            Paragraph::new(mic_meter_line(app))
                .alignment(Alignment::Right)
                .style(Style::default().bg(theme::PANEL_BG)),
            meter_rect,
        );
    }

    if inner.width < 10 || inner.height < 6 {
        return;
    }

    let active_turn = app.selected_trace().map(|t| t.id.clone()).unwrap_or_default();
    let extra_events = app
        .events
        .iter()
        .filter(|ev| app.log_filter.matches(ev))
        .filter(|ev| active_turn.is_empty() || ev.text("turn_id") != active_turn)
        .count();
    let event_h = (extra_events.max(1) as u16 + 1).min(8).min(inner.height / 3);
    let log_h = if inner.height >= 24 { 8 } else { 4 };

    let parts = Layout::vertical([
        Constraint::Min(6),
        Constraint::Length(event_h),
        Constraint::Length(log_h),
    ])
    .split(inner);

    draw_turn_xray(frame, app, parts[0], focused);
    draw_event_stream(frame, app, parts[1]);
    draw_daemon_log(frame, app, parts[2]);
}

fn draw_turn_xray(frame: &mut Frame, app: &App, area: Rect, focused: bool) {
    let Some(trace) = app.selected_trace() else {
        frame.render_widget(
            Paragraph::new(Line::from(Span::styled(
                "  say something: each stage lands here live",
                theme::dim(),
            )))
            .style(theme::base()),
            area,
        );
        return;
    };

    let width = area.width.saturating_sub(2).max(8) as usize;
    let lines = trace_lines(
        trace,
        app.sel_trace + 1,
        app.traces.len(),
        app.log_filter,
        width,
        focused,
    );
    let height = area.height as usize;
    let offset = if trace.done {
        0
    } else {
        lines.len().saturating_sub(height)
    };
    frame.render_widget(
        Paragraph::new(Text::from(lines))
            .style(theme::base())
            .scroll((offset.min(u16::MAX as usize) as u16, 0)),
        area,
    );
}

fn draw_event_stream(frame: &mut Frame, app: &App, area: Rect) {
    if area.height < 2 {
        return;
    }
    let active_turn = app.selected_trace().map(|t| t.id.as_str()).unwrap_or("");
    let shown = app
        .events
        .iter()
        .filter(|ev| app.log_filter.matches(ev))
        .count();
    let header = Line::from(vec![
        Span::styled("EVENTS ", theme::bold(theme::CYAN)),
        Span::styled(
            format!(
                "· {} · {shown}/{} (v/b/e/A)",
                app.log_filter.label(),
                app.events.len()
            ),
            theme::faint(),
        ),
    ]);
    let rows = area.height.saturating_sub(1) as usize;
    let mut lines = vec![header];
    lines.extend(event_lines(app, active_turn, area.width as usize, rows).lines);
    frame.render_widget(Paragraph::new(Text::from(lines)).style(theme::base()), area);
}

fn draw_daemon_log(frame: &mut Frame, app: &mut App, area: Rect) {
    if area.height < 2 {
        return;
    }
    let mut log_lines = vec![Line::from(Span::styled(
        "DAEMON LOG · jarvis.err.log",
        theme::bold(theme::PURPLE),
    ))];
    let tail = app.daemon_log();
    if tail.trim().is_empty() {
        log_lines.push(Line::from(Span::styled("  (empty or missing)", theme::dim())));
    } else {
        let take = area.height.saturating_sub(1) as usize;
        for raw in tail.lines().rev().take(take).collect::<Vec<_>>().into_iter().rev() {
            let style = if raw.contains("ERROR") || raw.contains("Traceback") {
                theme::fg(theme::DANGER)
            } else if raw.contains("WARNING") {
                theme::fg(theme::WARN)
            } else {
                theme::dim()
            };
            log_lines.push(Line::from(Span::styled(
                theme::clip_ellipsis(raw, area.width.saturating_sub(1) as usize),
                style,
            )));
        }
    }
    frame.render_widget(Paragraph::new(Text::from(log_lines)).style(theme::base()), area);
}

fn trace_lines(
    trace: &Trace,
    idx: usize,
    total: usize,
    filter: LogFilter,
    width: usize,
    focused: bool,
) -> Vec<Line<'static>> {
    let status = if trace.done { "done" } else { "live" };
    let mut lines = vec![
        Line::from(vec![
            Span::styled("turn   ", theme::faint()),
            Span::styled(
                format!("#{} ({idx}/{total}) · {} stages · {status}", trace.id, trace.stages.len()),
                theme::dim(),
            ),
        ]),
        Line::from(vec![
            Span::styled("heard  ", theme::faint()),
            Span::styled(theme::clip_ellipsis(&trace.headline(), width), theme::bold(theme::TEXT)),
        ]),
        Line::from(vec![
            Span::styled("spoke  ", theme::faint()),
            Span::styled(
                theme::clip_ellipsis(
                    &trace.spoken().map(|s| s.text.clone()).unwrap_or_else(|| "—".to_string()),
                    width,
                ),
                theme::fg(theme::ACCENT),
            ),
        ]),
        Line::from(""),
    ];
    for stage in trace.stages.iter().filter(|s| filter.matches_stage(s)) {
        lines.extend(stage_lines(stage, trace.started, width, focused));
    }
    lines
}

fn stage_lines(stage: &TraceStage, started: f64, width: usize, focused: bool) -> Vec<Line<'static>> {
    let (style, bar) = tone_style(stage.tone);
    let offset = (stage.ts - started).max(0.0);
    let mut header = vec![
        Span::styled(format!("{:<9}", stage.kind), style),
        Span::styled(format!("+{offset:.2}s "), theme::faint()),
    ];
    if !stage.detail.is_empty() {
        header.push(Span::styled(
            theme::clip_ellipsis(&stage.detail, width.saturating_sub(18)),
            theme::dim(),
        ));
    }
    let mut lines = vec![Line::from(
        std::iter::once(Span::styled(bar, style)).chain(header).collect::<Vec<_>>(),
    )];
    let body_style = if focused {
        theme::fg(theme::TEXT)
    } else {
        theme::dim()
    };
    for chunk in wrap_text(&stage.text, width.saturating_sub(2).max(4)).into_iter().take(3) {
        lines.push(Line::from(Span::styled(format!("  {chunk}"), body_style)));
    }
    lines
}

fn tone_style(tone: StageTone) -> (Style, &'static str) {
    match tone {
        StageTone::Ok => (theme::fg(theme::ACCENT), "▎"),
        StageTone::Think => (theme::fg(theme::PURPLE), "▎"),
        StageTone::Return => (theme::fg(theme::WARN), "▎"),
        StageTone::Bad => (theme::fg(theme::DANGER), "▎"),
        StageTone::Info => (theme::fg(theme::BLUE), "▎"),
    }
}

/// Builds the newest `height` event lines, skipping stages already expanded in `active_turn` so nothing repeats.
fn event_lines(app: &App, active_turn: &str, width: usize, height: usize) -> Text<'static> {
    let matching: Vec<&TelemetryEvent> = app
        .events
        .iter()
        .filter(|ev| app.log_filter.matches(ev))
        .filter(|ev| active_turn.is_empty() || ev.text("turn_id") != active_turn)
        .collect();
    if matching.is_empty() {
        let msg = if app.events.is_empty() {
            "  no events yet"
        } else {
            "  (other turns & brain events appear here)"
        };
        return Text::from(Line::from(Span::styled(msg, theme::dim())));
    }
    let window = &matching[matching.len().saturating_sub(height.max(1))..];
    let mut lines: Vec<Line<'static>> = Vec::with_capacity(window.len());
    for ev in window {
        let (label, style) = kind_style(&ev.event);
        let mut spans = vec![
            Span::styled(format!("{:>5} ", ev.seq), theme::faint()),
            Span::styled(format!("{label:<8}"), style),
        ];
        let main = primary_field(ev);
        if !main.is_empty() {
            spans.push(Span::styled(
                theme::clip_ellipsis(&main, width.saturating_sub(16)),
                theme::fg(theme::TEXT),
            ));
        }
        lines.push(Line::from(spans));
    }
    Text::from(lines)
}

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
        _ => return (theme::clip(event, 8), theme::dim()),
    };
    (label.to_string(), style)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::app::AppEvent;
    use crate::config::Config;
    use std::path::PathBuf;
    use std::sync::{mpsc, Arc, Mutex};

    #[test]
    fn event_lines_keeps_newest_rows_and_skips_active_turn_duplicates() {
        let (tx, _rx) = mpsc::channel();
        let cfg = Config {
            home: PathBuf::from("/tmp"),
            db_path: PathBuf::from("/tmp/state.db"),
            sessions_dir: PathBuf::from("/tmp/sessions"),
            traces_dir: PathBuf::from("/tmp/traces"),
            skills_dir: PathBuf::from("/tmp/skills"),
            notes_dir: PathBuf::from("/tmp/notes"),
            logs_dir: PathBuf::from("/tmp/logs"),
            host: "127.0.0.1".into(),
            port: 8643,
            api_key: "k".into(),
        };
        let mut app = App::new(cfg, tx, Arc::new(Mutex::new(None)));
        for seq in 1..=10 {
            app.apply(AppEvent::Telemetry(TelemetryEvent::from_value(serde_json::json!({
                "seq": seq, "ts": seq as f64, "event": "voice.stt", "turn_id": format!("t{seq}"), "text": format!("msg-{seq}")
            }))));
        }
        let rendered = event_lines(&app, "t10", 80, 3);
        assert_eq!(rendered.lines.len(), 3);
        let tail = format!("{:?}", rendered.lines.last().unwrap());
        assert!(tail.contains("msg-9"), "t10 is already shown in the X-Ray above: {tail}");
    }
}

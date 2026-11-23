//! Pane 2 — one turn from the user's sentence to the speaker, stage by stage.
//!
//! Every line is a real stage the voice published on the telemetry bus: what Whisper heard and how the voice
//! check scored it, the intent verdict, the cleanup rewrite, the memories that were injected, the prepared
//! request, the model's reasoning, the tool it called, what the runner returned, and what was spoken.

use ratatui::layout::Rect;
use ratatui::style::Style;
use ratatui::text::{Line, Span, Text};
use ratatui::widgets::Paragraph;
use ratatui::Frame;

use crate::app::{App, Pane};
use crate::model::{StageTone, Trace, TraceStage};
use crate::ui::grid::wrap_text;
use crate::ui::theme;

pub fn draw(frame: &mut Frame, app: &App, area: Rect) {
    let focused = app.focus == Pane::Xray;
    let title = match app.selected_trace() {
        Some(trace) => format!(
            "2 TURN X-RAY · #{} · {} stages · {}",
            trace.id,
            trace.stages.len(),
            if trace.done { "done" } else { "live" }
        ),
        None => "2 TURN X-RAY · waiting for a sentence".to_string(),
    };
    let block = theme::panel(focused, &title);
    let inner = block.inner(area);
    frame.render_widget(block, area);
    if inner.width < 8 || inner.height < 3 {
        return;
    }

    let Some(trace) = app.selected_trace() else {
        frame.render_widget(
            Paragraph::new(Line::from(Span::styled(
                "  say something: each stage of the turn lands here as it happens",
                theme::dim(),
            )))
            .style(theme::base()),
            inner,
        );
        return;
    };

    let width = inner.width.saturating_sub(2).max(8) as usize;
    let lines = trace_lines(trace, width, focused);
    let height = inner.height as usize;
    // A finished turn reads from the top; a live one follows its newest stage.
    let offset = if trace.done {
        0
    } else {
        lines.len().saturating_sub(height)
    };
    frame.render_widget(
        Paragraph::new(Text::from(lines))
            .style(theme::base())
            .scroll((offset.min(u16::MAX as usize) as u16, 0)),
        inner,
    );
}

fn trace_lines(trace: &Trace, width: usize, focused: bool) -> Vec<Line<'static>> {
    let mut lines = vec![
        Line::from(vec![
            Span::styled("heard  ".to_string(), theme::faint()),
            Span::styled(trace.headline(), theme::bold(theme::TEXT)),
        ]),
        Line::from(vec![
            Span::styled("spoke  ", theme::faint()),
            Span::styled(
                trace
                    .spoken()
                    .map(|s| s.text.clone())
                    .unwrap_or_else(|| "—".to_string()),
                theme::fg(theme::ACCENT),
            ),
        ]),
        Line::from(""),
    ];
    for stage in &trace.stages {
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
            theme::clip_ellipsis(&stage.detail, width.saturating_sub(26)),
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
    for chunk in wrap_text(&stage.text, width.saturating_sub(2).max(4)).into_iter().take(6) {
        lines.push(Line::from(Span::styled(format!("  {chunk}"), body_style)));
    }
    lines.push(Line::from(""));
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

//! Pane 2 — live and historical prompt / response / tool-call transcript inspector.

use ratatui::layout::Rect;
use ratatui::style::Style;
use ratatui::text::{Line, Span, Text};
use ratatui::widgets::Paragraph;
use ratatui::Frame;

use crate::app::{App, Pane};
use crate::model::{Turn, TurnKind};
use crate::ui::grid::wrap_text;
use crate::ui::theme;

pub fn draw(frame: &mut Frame, app: &App, area: Rect) {
    let focused = app.focus == Pane::Prompts;
    let file = app
        .session_path
        .as_ref()
        .and_then(|p| p.file_name())
        .and_then(|n| n.to_str())
        .unwrap_or("no session");
    let title = format!(
        "PROMPTS · {} · {} turns · follow:{}",
        theme::clip_ellipsis(file, 26),
        app.turns.len(),
        if app.follow { "on" } else { "off" }
    );
    let block = theme::panel(focused, &title);
    let inner = block.inner(area);
    frame.render_widget(block, area);
    if inner.width < 8 || inner.height < 3 {
        return;
    }

    let width = inner.width.saturating_sub(2) as usize;
    let mut lines: Vec<Line<'static>> = Vec::new();
    let mut block_starts: Vec<(usize, usize)> = Vec::new(); // (turn_idx, start_line)

    if app.turns.is_empty() {
        lines.push(Line::from(Span::styled(
            "  waiting for a pi session… (start a run, or press Enter on an attempt)",
            theme::dim(),
        )));
    }

    for (idx, turn) in app.turns.iter().enumerate() {
        block_starts.push((idx, lines.len()));
        let selected = idx == app.sel_prompts && focused;
        let expanded = app.expanded == Some(idx)
            || (app.follow && idx + 1 == app.turns.len())
            || app.expanded.is_none() && idx + 1 == app.turns.len();
        lines.extend(render_turn(turn, width, selected, expanded));
        lines.push(Line::from(""));
    }

    let height = inner.height as usize;
    let total = lines.len();
    let offset = if app.follow {
        total.saturating_sub(height)
    } else {
        let (_, start) = block_starts
            .iter()
            .find(|(idx, _)| *idx == app.sel_prompts)
            .copied()
            .unwrap_or((0, 0));
        let end = block_starts
            .iter()
            .find(|(idx, _)| *idx == app.sel_prompts + 1)
            .map(|(_, start)| *start)
            .unwrap_or(total);
        let block_len = end.saturating_sub(start).max(1);
        let mut offset = 0usize;
        if start + block_len > height {
            offset = start + block_len - height;
        }
        offset.min(total.saturating_sub(1))
    };

    let scroll = offset.min(u16::MAX as usize) as u16;
    frame.render_widget(
        Paragraph::new(Text::from(lines))
            .style(theme::base())
            .scroll((scroll, 0)),
        inner,
    );
}

fn render_turn(
    turn: &Turn,
    width: usize,
    selected: bool,
    expanded: bool,
) -> Vec<Line<'static>> {
    let (label, label_style, body_style) = match turn.kind {
        TurnKind::User => ("USER", theme::bold(theme::CYAN), theme::fg(theme::TEXT)),
        TurnKind::Assistant => ("ASSISTANT", theme::bold(theme::ACCENT), theme::fg(theme::TEXT)),
        TurnKind::Tool => ("TOOL", theme::bold(theme::PURPLE), theme::fg(theme::BLUE)),
        TurnKind::ToolResult => {
            if turn.is_error {
                ("ERROR", theme::bold(theme::DANGER), theme::fg(theme::DANGER))
            } else {
                ("RESULT", theme::dim(), theme::dim())
            }
        }
        TurnKind::System => ("SYSTEM", theme::bold(theme::WARN), theme::fg(theme::WARN)),
        TurnKind::Meta => ("SESSION", theme::faint(), theme::faint()),
    };

    let marker = if selected { "▌" } else { " " };
    let mut header: Vec<Span<'static>> = vec![
        Span::styled(marker.to_string(), theme::fg(theme::CYAN)),
        Span::styled(format!("{label:<9}"), label_style),
    ];
    if !turn.title.is_empty() && turn.title != label {
        header.push(Span::styled(
            theme::clip_ellipsis(&turn.title, width.saturating_sub(20)),
            theme::dim(),
        ));
    }
    if turn.cost > 0.0 {
        header.push(Span::styled(
            format!("  ${:.4}", turn.cost),
            theme::fg(theme::WARN),
        ));
    }
    let mut out = vec![Line::from(header)];

    let wrapped = wrap_text(&turn.body, width.saturating_sub(2).max(4));
    let limit = if expanded { wrapped.len() } else { wrapped.len().min(5) };
    for line in wrapped.iter().take(limit) {
        out.extend(style_body_line(line, body_style));
    }
    if wrapped.len() > limit {
        out.push(Line::from(Span::styled(
            format!("  … {} more lines (Enter to expand)", wrapped.len() - limit),
            theme::faint(),
        )));
    }
    out
}

/// Markdown-lite: code fences, headings, bullets, and diff coloring.
fn style_body_line(line: &str, base: Style) -> Vec<Line<'static>> {
    let trimmed = line.trim_start();
    if trimmed.starts_with("```") {
        return vec![Line::from(Span::styled(
            "  ─── code ───".to_string(),
            theme::faint(),
        ))];
    }
    let style = if trimmed.starts_with('#') {
        theme::bold(theme::ACCENT)
    } else if trimmed.starts_with('+') {
        theme::fg(theme::ACCENT)
    } else if trimmed.starts_with('-') {
        theme::fg(theme::DANGER)
    } else if trimmed.starts_with('!') {
        theme::fg(theme::WARN)
    } else {
        base
    };
    let text = line.replace("**", "");
    vec![Line::from(Span::styled(format!("  {text}"), style))]
}

/// Renders one attempt's verify output / diagnostic for the attempt modal.
pub fn attempt_lines(app: &App, attempt_id: i64) -> Vec<Line<'static>> {
    let Some(attempt) = app.snapshot.attempts.iter().find(|a| a.id == attempt_id) else {
        return vec![Line::from(Span::styled("attempt not found", theme::dim()))];
    };
    let mut lines = Vec::new();

    fn kv(lines: &mut Vec<Line<'static>>, label: &str, value: String, style: Style) {
        lines.push(Line::from(vec![
            Span::styled(format!("{label:<14}"), theme::dim()),
            Span::styled(value, style),
        ]));
    }

    kv(&mut lines, "attempt", format!("#{}", attempt.id), theme::fg(theme::TEXT));
    kv(&mut lines, "task", attempt.task_id.clone(), theme::fg(theme::CYAN));
    kv(&mut lines, "role", attempt.role.clone(), theme::fg(theme::PURPLE));
    kv(&mut lines, "outcome", attempt.outcome.clone(), theme::outcome_style(&attempt.outcome));
    kv(&mut lines, "cost", format!("${:.4}", attempt.cost_usd), theme::fg(theme::WARN));
    if let Some(metric) = attempt.metric_value {
        kv(&mut lines, "metric", format!("{metric}"), theme::fg(theme::ACCENT));
    }
    if let Some(branch) = &attempt.git_branch {
        kv(&mut lines, "branch", branch.clone(), theme::fg(theme::BLUE));
    }
    if let Some(sha) = &attempt.git_sha {
        kv(&mut lines, "sha", sha.clone(), theme::dim());
    }
    kv(&mut lines, "reflected", attempt.reflected.to_string(), theme::dim());
    kv(&mut lines, "created", attempt.created_at.clone(), theme::faint());
    if let Some(session) = &attempt.pi_session_id {
        kv(&mut lines, "pi session", session.clone(), theme::dim());
    }
    if !attempt.skills_used.is_empty() {
        kv(&mut lines, "skills", attempt.skills_used.join(", "), theme::fg(theme::ACCENT));
    }
    lines.push(Line::from(""));
    lines.push(Line::from(Span::styled("SUMMARY", theme::bold(theme::PURPLE))));
    for chunk in wrap_text(&attempt.summary, 72) {
        lines.push(Line::from(Span::styled(chunk, theme::fg(theme::TEXT))));
    }
    if let Some(diag) = &attempt.diagnostic {
        lines.push(Line::from(""));
        lines.push(Line::from(Span::styled("DIAGNOSTIC", theme::bold(theme::WARN))));
        for chunk in wrap_text(diag, 72) {
            lines.push(Line::from(Span::styled(chunk, theme::fg(theme::WARN))));
        }
    }
    if let Some(output) = &attempt.verify_output {
        lines.push(Line::from(""));
        lines.push(Line::from(Span::styled("VERIFY OUTPUT", theme::bold(theme::PURPLE))));
        let tail: Vec<&str> = output.lines().rev().take(40).collect();
        for raw in tail.into_iter().rev() {
            let style = if raw.contains("FAILED") || raw.contains("error") || raw.contains("Error") {
                theme::fg(theme::DANGER)
            } else {
                theme::dim()
            };
            lines.push(Line::from(Span::styled(theme::clip_ellipsis(raw, 74), style)));
        }
    }
    lines
}

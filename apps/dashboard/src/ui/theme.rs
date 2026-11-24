//! Cyberdeck-inspired dark palette and shared text styling helpers.

use ratatui::style::{Color, Modifier, Style};
use ratatui::widgets::{Block, Borders};

pub const BG: Color = Color::Rgb(9, 11, 17);
pub const PANEL_BG: Color = Color::Rgb(13, 16, 24);
pub const BORDER: Color = Color::Rgb(46, 56, 76);
pub const BORDER_FOCUS: Color = Color::Rgb(88, 196, 250);
pub const TEXT: Color = Color::Rgb(198, 208, 222);
pub const DIM: Color = Color::Rgb(108, 120, 138);
pub const FAINT: Color = Color::Rgb(74, 84, 100);
pub const ACCENT: Color = Color::Rgb(112, 222, 168);
pub const WARN: Color = Color::Rgb(242, 192, 96);
pub const DANGER: Color = Color::Rgb(242, 112, 112);
pub const CYAN: Color = Color::Rgb(88, 196, 250);
pub const BLUE: Color = Color::Rgb(112, 160, 246);
pub const PURPLE: Color = Color::Rgb(182, 150, 250);

pub fn base() -> Style {
    Style::default().fg(TEXT).bg(BG)
}

pub fn fg(color: Color) -> Style {
    Style::default().fg(color)
}

pub fn dim() -> Style {
    Style::default().fg(DIM)
}

pub fn faint() -> Style {
    Style::default().fg(FAINT)
}

pub fn bold(color: Color) -> Style {
    Style::default().fg(color).add_modifier(Modifier::BOLD)
}

pub fn panel(focused: bool, title: &str) -> Block<'static> {
    let border_style = if focused {
        Style::default().fg(BORDER_FOCUS).add_modifier(Modifier::BOLD)
    } else {
        Style::default().fg(BORDER)
    };
    Block::default()
        .borders(Borders::ALL)
        .border_style(border_style)
        .title(ratatui::text::Line::from(ratatui::text::Span::styled(
            format!(" {title} "),
            if focused {
                bold(BORDER_FOCUS)
            } else {
                dim()
            },
        )))
        .style(Style::default().bg(PANEL_BG))
}

pub fn status_style(status: &str) -> Style {
    match status {
        "done" | "pushed" | "approved" | "active" | "decomposed" => fg(ACCENT),
        "in_progress" | "running" | "queued" | "ready" => fg(CYAN),
        "failed_verify" | "errored" | "blocked" | "timed_out" | "rejected" => fg(DANGER),
        "deferred_approval" | "reaped" | "draft" | "spoken" => fg(WARN),
        "retired" => fg(FAINT),
        _ => fg(TEXT),
    }
}

pub fn outcome_style(outcome: &str) -> Style {
    status_style(outcome)
}

pub fn bytes_human(bytes: u64) -> String {
    const UNITS: [&str; 4] = ["B", "KB", "MB", "GB"];
    let mut value = bytes as f64;
    let mut unit = 0;
    while value >= 1024.0 && unit < UNITS.len() - 1 {
        value /= 1024.0;
        unit += 1;
    }
    if unit == 0 {
        format!("{bytes} B")
    } else {
        format!("{value:.1} {}", UNITS[unit])
    }
}

/// Truncates by character count (not bytes) and appends an ellipsis when clipped.
pub fn clip(text: &str, width: usize) -> String {
    if width == 0 {
        return String::new();
    }
    let mut out = String::new();
    for ch in text.chars() {
        if out.chars().count() >= width {
            return out;
        }
        out.push(ch);
    }
    out
}

pub fn clip_ellipsis(text: &str, width: usize) -> String {
    if text.chars().count() <= width {
        return text.to_string();
    }
    if width == 0 {
        return String::new();
    }
    let mut out: String = text.chars().take(width.saturating_sub(1)).collect();
    out.push('…');
    out
}


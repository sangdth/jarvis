//! Pane 6 — recognized and heard speakers (saved roster merged with live voice telemetry).

use ratatui::layout::Rect;
use ratatui::style::Style;
use ratatui::text::{Line, Span};
use ratatui::widgets::Paragraph;
use ratatui::Frame;

use crate::app::{now_unix, App, Pane};
use crate::model::Speaker;
use crate::ui::grid::scroll_offset;
use crate::ui::theme;

pub fn draw(frame: &mut Frame, app: &App, area: Rect) {
    let focused = app.focus == Pane::Speakers;
    let known = app.speakers.iter().filter(|s| s.known || s.enrolled).count();
    let title = format!(
        "6 SPEAKERS · {known} known · {} total · {}",
        app.speakers.len(),
        app.check_mode
    );
    let block = theme::panel(focused, &title);
    let inner = block.inner(area);
    frame.render_widget(block, area);
    if inner.width < 20 || inner.height < 2 {
        return;
    }

    let learning_owner = app.check_mode == "learning owner";
    let show_profile = inner.width >= 74;
    let height = inner.height as usize;
    let body_rows = height.saturating_sub(1);
    let offset = scroll_offset(app.sel_speakers, app.speakers.len(), body_rows);
    let now = now_unix();

    let mut header = vec![
        Span::styled(format!("{:<15}", " speaker"), theme::faint()),
        Span::styled(format!("{:<10}", "status"), theme::faint()),
        Span::styled(format!("{:>6}", "heard"), theme::faint()),
        Span::styled(format!("{:>6}", "turns"), theme::faint()),
        Span::styled(format!("{:>5}", "drop"), theme::faint()),
        Span::styled(format!("{:>6}", "conf"), theme::faint()),
        Span::styled(format!("{:>6}", "sim"), theme::faint()),
    ];
    if show_profile {
        header.push(Span::styled(format!("  {:<10}", "profile"), theme::faint()));
    }
    header.push(Span::styled("  last", theme::faint()));

    let mut lines: Vec<Line<'static>> = vec![Line::from(header)];
    if app.speakers.is_empty() {
        lines.push(Line::from(Span::styled(
            "  no voices heard yet — talk to Jarvis or enroll speakers",
            theme::dim(),
        )));
    }

    for (row_idx, speaker) in app
        .speakers
        .iter()
        .enumerate()
        .skip(offset)
        .take(body_rows)
    {
        let selected = row_idx == app.sel_speakers && focused;
        let marker = if selected { "▌" } else { " " };
        let status = speaker.status_label(learning_owner);
        let status_style = speaker_status_style(speaker, learning_owner);
        let name_style = if selected {
            theme::bold(theme::TEXT)
        } else if speaker.is_owner {
            theme::bold(theme::ACCENT)
        } else if speaker.enrolled || speaker.known {
            theme::fg(theme::TEXT)
        } else {
            theme::dim()
        };
        let drop_style = if speaker.dropped > 0 {
            theme::fg(theme::DANGER)
        } else {
            theme::faint()
        };
        let conf = format_conf(speaker, learning_owner);
        let sim = format_table_sim(speaker);
        let last = format_last(speaker, now);

        let mut spans = vec![
            Span::styled(
                format!("{marker}{:<14}", theme::clip(speaker.display_name(), 14)),
                name_style,
            ),
            Span::styled(format!("{:<10}", theme::clip(&status, 10)), status_style),
            Span::styled(format!("{:>6}", speaker.heard), theme::fg(theme::TEXT)),
            Span::styled(format!("{:>6}", speaker.turns), theme::fg(theme::CYAN)),
            Span::styled(format!("{:>5}", speaker.dropped), drop_style),
            Span::styled(format!("{:>6}", conf), theme::dim()),
            Span::styled(format!("{:>6}", sim), theme::dim()),
        ];
        if show_profile {
            let profile = if speaker.is_owner && app.check_mode == "any voice" {
                "check off".to_string()
            } else {
                speaker.profile_label()
            };
            spans.push(Span::styled(
                format!("  {:<10}", theme::clip(&profile, 10)),
                theme::faint(),
            ));
        }
        spans.push(Span::styled(format!("  {last}"), theme::faint()));

        let mut line = Line::from(spans);
        if selected {
            line = line.style(Style::default().bg(theme::PANEL_BG));
        }
        lines.push(line);
    }

    frame.render_widget(Paragraph::new(lines).style(theme::base()), inner);
}

pub fn speaker_modal_lines(app: &App, id: &str) -> Vec<Line<'static>> {
    let Some(speaker) = app.speaker_by_id(id) else {
        return vec![Line::from(Span::styled(
            format!("  speaker {id} is no longer in the table"),
            theme::dim(),
        ))];
    };
    let learning_owner = app.check_mode == "learning owner";
    let status = speaker.status_label(learning_owner);
    let sim_line = match (speaker.last_sim, speaker.min_sim, speaker.max_sim) {
        (Some(last), Some(min), Some(max)) => {
            format!("{last:.2}  (min {min:.2} .. max {max:.2}, cosine vs owner)")
        }
        (Some(last), _, _) => format!("{last:.2} (cosine vs owner)"),
        _ => "—".to_string(),
    };
    let voiceprint = if speaker.is_owner {
        if speaker.enrolled {
            format!("saved ({})", app.cfg.owner_voiceprint_path().display())
        } else {
            format!("not saved yet ({})", app.cfg.owner_voiceprint_path().display())
        }
    } else if speaker.enrolled {
        format!(
            "{} clips · {:.1}s · vouched={} ({})",
            speaker.clips,
            speaker.speech_secs,
            if speaker.vouched { "yes" } else { "no" },
            app.cfg.speakers_path().display()
        )
    } else {
        "in RAM only (unnamed voices expire after 10m idle)".to_string()
    };

    vec![
        Line::from(""),
        kv("speaker", speaker.display_name(), theme::bold(theme::TEXT)),
        kv("profile id", &speaker.id, theme::fg(theme::CYAN)),
        kv("status", &status, speaker_status_style(speaker, learning_owner)),
        kv(
            "activity",
            &format!(
                "{} heard · {} turns · {} dropped",
                speaker.heard, speaker.turns, speaker.dropped
            ),
            theme::fg(theme::TEXT),
        ),
        kv("belief", &format_conf(speaker, learning_owner), theme::dim()),
        kv("vs owner", &sim_line, theme::dim()),
        kv("voiceprint", &voiceprint, theme::dim()),
        kv("last heard", &format_last(speaker, now_unix()), theme::dim()),
        Line::from(""),
        Line::from(Span::styled(
            "  Esc or Enter to close",
            theme::faint(),
        )),
    ]
}

fn kv(label: &str, value: &str, style: Style) -> Line<'static> {
    Line::from(vec![
        Span::styled(format!("  {label:<12}"), theme::faint()),
        Span::styled(value.to_string(), style),
    ])
}

fn speaker_status_style(speaker: &Speaker, learning_owner: bool) -> Style {
    if speaker.is_owner {
        if learning_owner {
            theme::bold(theme::WARN)
        } else {
            theme::bold(theme::ACCENT)
        }
    } else if speaker.enrolled || speaker.known {
        theme::fg(theme::CYAN)
    } else if speaker.confidence.is_some_and(|c| c >= 0.50) {
        theme::fg(theme::WARN)
    } else {
        theme::dim()
    }
}

fn format_conf(speaker: &Speaker, learning_owner: bool) -> String {
    if speaker.is_owner {
        if learning_owner {
            "—".to_string()
        } else {
            "100%".to_string()
        }
    } else if speaker.enrolled {
        "100%".to_string()
    } else if let Some(conf) = speaker.confidence {
        if conf > 0.0 {
            format!("{:.0}%", conf * 100.0)
        } else {
            "—".to_string()
        }
    } else {
        "—".to_string()
    }
}

/// Only show owner-cosine similarity for the owner and unidentified voices; on named guests it measures
/// dissimilarity from the owner and would read like a broken guest match.
fn format_table_sim(speaker: &Speaker) -> String {
    if !speaker.is_owner && (speaker.enrolled || speaker.known) {
        return "—".to_string();
    }
    speaker
        .last_sim
        .map(|s| format!("{s:.2}"))
        .unwrap_or_else(|| "—".to_string())
}

fn format_last(speaker: &Speaker, now: f64) -> String {
    let Some(ts) = speaker.last_ts else {
        return if speaker.enrolled || speaker.is_owner {
            "saved".to_string()
        } else {
            "—".to_string()
        };
    };
    if ts < 1_000_000_000.0 {
        return "live".to_string();
    }
    let age = (now - ts).max(0.0) as u64;
    if age < 5 {
        "now".to_string()
    } else if age < 60 {
        format!("{age}s ago")
    } else if age < 3600 {
        format!("{}m ago", age / 60)
    } else if age < 86_400 {
        format!("{}h ago", age / 3600)
    } else {
        format!("{}d ago", age / 86_400)
    }
}



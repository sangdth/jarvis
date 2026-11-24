//! Pane 4 — the durable ledger: attempts, skills, outbox, progress notes, DB health & logs.

use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::text::{Line, Span};
use ratatui::widgets::Paragraph;
use ratatui::Frame;

use crate::app::{App, LedgerTab, Pane};
use crate::ui::grid::{scroll_offset, wrap_text};
use crate::ui::theme;

pub fn draw(frame: &mut Frame, app: &mut App, area: Rect) {
    let focused = app.focus == Pane::Ledger;
    let title = format!("4 LEDGER · {}", app.ledger_tab.label());
    let block = theme::panel(focused, &title);
    let inner = block.inner(area);
    frame.render_widget(block, area);
    if inner.width < 12 || inner.height < 3 {
        return;
    }

    let chunks = Layout::vertical([Constraint::Length(1), Constraint::Min(1)]).split(inner);
    frame.render_widget(Paragraph::new(tab_bar(app)).style(theme::base()), chunks[0]);

    match app.ledger_tab {
        LedgerTab::Attempts => draw_attempts(frame, app, chunks[1]),
        LedgerTab::Skills => draw_skills(frame, app, chunks[1]),
        LedgerTab::Outbox => draw_outbox(frame, app, chunks[1]),
        LedgerTab::Notes => draw_notes(frame, app, chunks[1]),
        LedgerTab::Database => draw_database(frame, app, chunks[1]),
    }
}

fn tab_bar(app: &App) -> Line<'static> {
    let mut spans = Vec::new();
    for (idx, tab) in LedgerTab::ALL.iter().enumerate() {
        let label = match tab {
            LedgerTab::Attempts => format!("Attempts({})", app.snapshot.attempts.len()),
            LedgerTab::Skills => format!("Skills({}/{})", app.snapshot.counts.skills_active, app.snapshot.counts.skills_total),
            LedgerTab::Outbox => {
                let queued = app.snapshot.counts.approvals_queued;
                if queued > 0 {
                    format!("Outbox({queued}!)")
                } else {
                    format!("Outbox({})", app.snapshot.outbox.len())
                }
            }
            LedgerTab::Notes => "Notes".to_string(),
            LedgerTab::Database => "DB & Logs".to_string(),
        };
        let style = if *tab == app.ledger_tab {
            theme::bold(theme::CYAN)
        } else {
            theme::dim()
        };
        spans.push(Span::styled(format!(" {} ", if *tab == app.ledger_tab { format!("▸{label}") } else { format!(" {label}") }), style));
        if idx + 1 < LedgerTab::ALL.len() {
            spans.push(Span::styled("│", theme::faint()));
        }
    }
    spans.push(Span::styled("   [ / ] switch", theme::faint()));
    Line::from(spans)
}

fn draw_attempts(frame: &mut Frame, app: &App, area: Rect) {
    let height = area.height as usize;
    let offset = scroll_offset(app.sel_attempts, app.snapshot.attempts.len(), height);
    let width = area.width as usize;
    let mut lines: Vec<Line<'static>> = Vec::new();
    lines.push(Line::from(vec![
        Span::styled(format!("{:<5}", "#"), theme::faint()),
        Span::styled(format!("{:<9}", "task"), theme::faint()),
        Span::styled(format!("{:<8}", "role"), theme::faint()),
        Span::styled(format!("{:<16}", "outcome"), theme::faint()),
        Span::styled(format!("{:>8}", "cost"), theme::faint()),
        Span::styled(format!("{:>9}", "metric"), theme::faint()),
        Span::styled("  summary", theme::faint()),
    ]));
    for (row_idx, attempt) in app.snapshot.attempts.iter().enumerate().skip(offset).take(height.saturating_sub(1)) {
        let selected = row_idx == app.sel_attempts && app.focus == Pane::Ledger;
        let marker = if selected { "▌" } else { " " };
        let summary_width = width.saturating_sub(60);
        let mut line = Line::from(vec![
            Span::styled(format!("{marker}{:<4}", attempt.id), theme::faint()),
            Span::styled(format!("{:<9}", theme::clip(&attempt.task_id, 9)), theme::bold(theme::TEXT)),
            Span::styled(format!("{:<8}", theme::clip(&attempt.role, 8)), theme::fg(theme::PURPLE)),
            Span::styled(
                format!("{:<16}", theme::clip(&attempt.outcome, 16)),
                theme::outcome_style(&attempt.outcome),
            ),
            Span::styled(format!("{:>8}", format!("${:.4}", attempt.cost_usd)), theme::fg(theme::WARN)),
            Span::styled(
                format!("{:>9}", attempt.metric_value.map(|m| m.to_string()).unwrap_or_else(|| "—".into())),
                theme::fg(theme::ACCENT),
            ),
            Span::styled(
                format!("  {}", theme::clip_ellipsis(&attempt.summary, summary_width)),
                if selected { theme::bold(theme::TEXT) } else { theme::dim() },
            ),
        ]);
        if selected {
            line = line.style(ratatui::style::Style::default().bg(theme::PANEL_BG));
        }
        lines.push(line);
    }
    frame.render_widget(Paragraph::new(lines).style(theme::base()), area);
}

fn draw_skills(frame: &mut Frame, app: &App, area: Rect) {
    let height = area.height as usize;
    let offset = scroll_offset(app.sel_skills, app.snapshot.skills.len(), height);
    let width = area.width as usize;
    let mut lines: Vec<Line<'static>> = Vec::new();
    lines.push(Line::from(vec![
        Span::styled(format!("{:<24}", "slug"), theme::faint()),
        Span::styled(format!("{:<4}", "v"), theme::faint()),
        Span::styled(format!("{:<8}", "status"), theme::faint()),
        Span::styled(format!("{:>5}", "loads"), theme::faint()),
        Span::styled(format!("{:>4}", "ok"), theme::faint()),
        Span::styled(format!("{:>5}", "fail"), theme::faint()),
        Span::styled("  description", theme::faint()),
    ]));
    for (row_idx, skill) in app.snapshot.skills.iter().enumerate().skip(offset).take(height.saturating_sub(1)) {
        let selected = row_idx == app.sel_skills && app.focus == Pane::Ledger;
        let marker = if selected { "▌" } else { " " };
        let desc_width = width.saturating_sub(55);
        let fail_style = if skill.failures_since_update >= 2 {
            theme::bold(theme::DANGER)
        } else if skill.failures_since_update == 1 {
            theme::fg(theme::WARN)
        } else {
            theme::dim()
        };
        let mut line = Line::from(vec![
            Span::styled(format!("{marker}{:<23}", theme::clip(&skill.slug, 23)), theme::bold(theme::TEXT)),
            Span::styled(format!("{:<4}", skill.version), theme::fg(theme::BLUE)),
            Span::styled(format!("{:<8}", theme::clip(&skill.status, 8)), theme::status_style(&skill.status)),
            Span::styled(format!("{:>5}", skill.loads_since_update), theme::dim()),
            Span::styled(format!("{:>4}", skill.successes_since_update), theme::fg(theme::ACCENT)),
            Span::styled(format!("{:>5}", skill.failures_since_update), fail_style),
            Span::styled(
                format!("  {}", theme::clip_ellipsis(&skill.description, desc_width)),
                if selected { theme::bold(theme::TEXT) } else { theme::dim() },
            ),
        ]);
        if selected {
            line = line.style(ratatui::style::Style::default().bg(theme::PANEL_BG));
        }
        lines.push(line);
    }
    frame.render_widget(Paragraph::new(lines).style(theme::base()), area);
}

fn draw_outbox(frame: &mut Frame, app: &App, area: Rect) {
    let height = area.height as usize;
    let offset = scroll_offset(app.sel_outbox, app.snapshot.outbox.len(), height);
    let width = area.width as usize;
    let mut lines: Vec<Line<'static>> = Vec::new();
    for (row_idx, item) in app.snapshot.outbox.iter().enumerate().skip(offset).take(height) {
        let selected = row_idx == app.sel_outbox && app.focus == Pane::Ledger;
        let marker = if selected { "▌" } else { " " };
        let actionable = item.kind == "approval_request" && item.status == "queued";
        let summary_width = width.saturating_sub(58);
        let mut line = Line::from(vec![
            Span::styled(format!("{marker}{:<12}", item.id), theme::faint()),
            Span::styled(format!("{:<18}", theme::clip(&item.kind, 18)), theme::fg(theme::PURPLE)),
            Span::styled(format!("{:<9}", theme::clip(&item.status, 9)), theme::status_style(&item.status)),
            Span::styled(
                format!("{:<9}", item.task_id.clone().unwrap_or_else(|| "—".into())),
                theme::fg(theme::CYAN),
            ),
            Span::styled(
                format!("{:<17}", theme::clip(&item.created_at, 16)),
                theme::faint(),
            ),
            Span::styled(
                theme::clip_ellipsis(&item.summary_spoken, summary_width),
                if actionable {
                    theme::bold(theme::WARN)
                } else {
                    theme::dim()
                },
            ),
        ]);
        if selected {
            line = line.style(ratatui::style::Style::default().bg(theme::PANEL_BG));
        }
        lines.push(line);
    }
    if app.snapshot.outbox.is_empty() {
        lines.push(Line::from(Span::styled("  outbox is empty", theme::dim())));
    }
    frame.render_widget(Paragraph::new(lines).style(theme::base()), area);
}

fn draw_notes(frame: &mut Frame, app: &mut App, area: Rect) {
    let task_id = app
        .selected_task()
        .map(|t| t.id.clone())
        .or_else(|| app.selected_attempt().map(|a| a.task_id.clone()))
        .unwrap_or_else(|| "SG-1".to_string());
    let note = app.note_for(&task_id);
    let width = area.width.saturating_sub(2) as usize;
    let mut lines: Vec<Line<'static>> = Vec::new();
    lines.push(Line::from(vec![
        Span::styled("progress note · ", theme::faint()),
        Span::styled(task_id, theme::bold(theme::CYAN)),
        Span::styled(
            format!(" · {} bytes", note.len()),
            theme::faint(),
        ),
    ]));
    if note.trim().is_empty() {
        lines.push(Line::from(Span::styled(
            "  (no handoff notes recorded for this task yet)",
            theme::dim(),
        )));
    } else {
        for raw in note.lines() {
            for chunk in wrap_text(raw, width) {
                let style = if chunk.starts_with("###") {
                    theme::bold(theme::PURPLE)
                } else if chunk.starts_with('-') {
                    theme::dim()
                } else {
                    theme::fg(theme::TEXT)
                };
                lines.push(Line::from(Span::styled(chunk, style)));
            }
        }
    }
    let height = area.height as usize;
    let offset = lines.len().saturating_sub(height);
    frame.render_widget(
        Paragraph::new(lines)
            .style(theme::base())
            .scroll((offset.min(u16::MAX as usize) as u16, 0)),
        area,
    );
}

fn draw_database(frame: &mut Frame, app: &mut App, area: Rect) {
    let width = area.width.saturating_sub(2) as usize;
    let mut lines: Vec<Line<'static>> = Vec::new();
    let counts = &app.snapshot.counts;
    lines.push(Line::from(vec![
        Span::styled("rows   ", theme::faint()),
        Span::styled(
            format!(
                "tasks {} · attempts {} · skills {}/{} · outbox {} ({} approvals)",
                counts.tasks,
                counts.attempts,
                counts.skills_active,
                counts.skills_total,
                app.snapshot.outbox.len(),
                counts.approvals_queued
            ),
            theme::fg(theme::TEXT),
        ),
    ]));
    lines.push(Line::from(vec![
        Span::styled("sqlite ", theme::faint()),
        Span::styled(
            format!(
                "db {} · wal {} · shm {} · pages {} · freelist {} · page_size {}",
                theme::bytes_human(app.snapshot.db_bytes),
                theme::bytes_human(app.snapshot.wal_bytes),
                theme::bytes_human(app.snapshot.shm_bytes),
                app.snapshot.page_count,
                app.snapshot.freelist_count,
                app.snapshot.page_size
            ),
            theme::fg(theme::TEXT),
        ),
    ]));
    lines.push(Line::from(vec![
        Span::styled("db     ", theme::faint()),
        Span::styled(app.cfg.db_path.display().to_string(), theme::dim()),
    ]));
    lines.push(Line::from(vec![
        Span::styled("sessions ", theme::faint()),
        Span::styled(app.cfg.sessions_dir.display().to_string(), theme::dim()),
    ]));
    lines.push(Line::from(vec![
        Span::styled("skills ", theme::faint()),
        Span::styled(app.cfg.skills_dir.display().to_string(), theme::dim()),
        Span::styled("  traces ", theme::faint()),
        Span::styled(app.cfg.traces_dir.display().to_string(), theme::dim()),
    ]));
    if let Some(err) = &app.snapshot_error {
        lines.push(Line::from(vec![
            Span::styled("read   ", theme::faint()),
            Span::styled(err.clone(), theme::fg(theme::DANGER)),
        ]));
    }
    lines.push(Line::from(""));
    lines.push(Line::from(Span::styled(
        "DAEMON LOG (tail · jarvis.err.log)",
        theme::bold(theme::PURPLE),
    )));
    let log = app.daemon_log();
    if log.trim().is_empty() {
        lines.push(Line::from(Span::styled("  (log is empty or missing)", theme::dim())));
    } else {
        for raw in log.lines().rev().take(area.height as usize).collect::<Vec<_>>().into_iter().rev() {
            let style = if raw.contains("ERROR") || raw.contains("Traceback") {
                theme::fg(theme::DANGER)
            } else if raw.contains("WARNING") {
                theme::fg(theme::WARN)
            } else {
                theme::dim()
            };
            lines.push(Line::from(Span::styled(
                theme::clip_ellipsis(raw, width),
                style,
            )));
        }
    }
    let height = area.height as usize;
    let offset = lines.len().saturating_sub(height);
    frame.render_widget(
        Paragraph::new(lines)
            .style(theme::base())
            .scroll((offset.min(u16::MAX as usize) as u16, 0)),
        area,
    );
}

/// Skill detail modal with a unified diff against the previous version.
pub fn skill_modal_lines(app: &App, slug: &str) -> Vec<Line<'static>> {
    use similar::{ChangeTag, TextDiff};

    let Some(skill) = app.snapshot.skills.iter().find(|s| s.slug == slug) else {
        return vec![Line::from(Span::styled("skill not found", theme::dim()))];
    };
    let mut lines = Vec::new();
    lines.push(Line::from(vec![
        Span::styled(skill.slug.clone(), theme::bold(theme::CYAN)),
        Span::styled(format!("  v{}", skill.version), theme::fg(theme::BLUE)),
        Span::styled(format!("  {}", skill.status), theme::status_style(&skill.status)),
    ]));
    for chunk in wrap_text(&skill.description, 74) {
        lines.push(Line::from(Span::styled(chunk, theme::fg(theme::TEXT))));
    }
    lines.push(Line::from(vec![
        Span::styled("name ", theme::faint()),
        Span::styled(skill.name.clone(), theme::fg(theme::TEXT)),
        Span::styled("   updated ", theme::faint()),
        Span::styled(skill.updated_at.clone(), theme::dim()),
    ]));
    if !skill.lesson.trim().is_empty() {
        for chunk in wrap_text(&format!("lesson: {}", skill.lesson), 74) {
            lines.push(Line::from(Span::styled(chunk, theme::fg(theme::WARN))));
        }
    }
    lines.push(Line::from(vec![
        Span::styled("sha256 ", theme::faint()),
        Span::styled(skill.sha256.clone(), theme::dim()),
    ]));
    lines.push(Line::from(vec![
        Span::styled("counters ", theme::faint()),
        Span::styled(
            format!(
                "loads {} · ok {} · fail {}",
                skill.loads_since_update, skill.successes_since_update, skill.failures_since_update
            ),
            theme::fg(theme::TEXT),
        ),
    ]));
    lines.push(Line::from(vec![
        Span::styled("evidence ", theme::faint()),
        Span::styled(
            format!("{:?}", skill.evidence_attempt_ids),
            theme::fg(theme::PURPLE),
        ),
    ]));
    lines.push(Line::from(""));

    if let Some(previous) = &skill.previous_content {
        lines.push(Line::from(Span::styled(
            "DIFF vs previous version",
            theme::bold(theme::PURPLE),
        )));
        let diff = TextDiff::from_lines(previous, &skill.content);
        for change in diff.iter_all_changes().take(160) {
            let (sign, style) = match change.tag() {
                ChangeTag::Delete => ("-", theme::fg(theme::DANGER)),
                ChangeTag::Insert => ("+", theme::fg(theme::ACCENT)),
                ChangeTag::Equal => (" ", theme::faint()),
            };
            lines.push(Line::from(Span::styled(
                format!("{sign}{}", theme::clip_ellipsis(change.value().trim_end(), 74)),
                style,
            )));
        }
    } else {
        lines.push(Line::from(Span::styled(
            "CURRENT SKILL BODY",
            theme::bold(theme::PURPLE),
        )));
        for raw in skill.content.lines().take(120) {
            lines.push(Line::from(Span::styled(
                theme::clip_ellipsis(raw, 74),
                theme::dim(),
            )));
        }
    }
    lines
}

//! Pane 3 — task DAG, auto-research hypothesis tournament, and metric convergence.

use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Paragraph, Sparkline};
use ratatui::Frame;

use crate::app::{flat_tasks, App, Pane};
use crate::ui::grid::wrap_text;
use crate::ui::theme;

pub fn draw(frame: &mut Frame, app: &App, area: Rect) {
    let focused = app.focus == Pane::Tasks;
    let ready = app.snapshot.tasks.iter().filter(|t| t.status == "ready").count();
    let blocked = app.snapshot.tasks.iter().filter(|t| t.status == "blocked").count();
    let title = format!(
        "3 TASKS · {} total · {} ready · {} blocked",
        app.snapshot.tasks.len(),
        ready,
        blocked
    );
    let block = theme::panel(focused, &title);
    let inner = block.inner(area);
    frame.render_widget(block, area);
    if inner.width < 12 || inner.height < 3 {
        return;
    }

    let rows = flat_tasks(&app.snapshot);
    let detail_height: u16 = if inner.height >= 12 { 5 } else { 0 };
    let chunks = Layout::vertical([
        Constraint::Min(3),
        Constraint::Length(detail_height),
    ])
    .split(inner);

    let list_height = chunks[0].height as usize;
    let offset = scroll_offset(app.sel_tasks, rows.len(), list_height);
    let mut lines: Vec<Line<'static>> = Vec::new();

    if rows.is_empty() {
        lines.push(Line::from(Span::styled(
            "  no tasks yet — `jarvis add ...` or ask the Planner to create one",
            theme::dim(),
        )));
    }

    for (row_idx, (snap_idx, depth)) in rows.iter().enumerate().skip(offset).take(list_height) {
        let Some(task) = app.snapshot.tasks.get(*snap_idx) else {
            continue;
        };
        let selected = row_idx == app.sel_tasks && focused;
        let indent = "  ".repeat(*depth);
        let marker = if selected { "▌" } else { " " };
        let mode_tag = if task.mode.is_empty() || task.mode == "AUTONOMOUS" {
            "AUTO"
        } else {
            &task.mode
        };
        let mut suffix = format!("  {} · {}", task.repo, mode_tag);
        if task.strike_count > 0 {
            suffix.push_str(&format!(" · strike {}/3", task.strike_count));
        }
        if task.child_strategy == "any" {
            suffix.push_str(" · ANY");
        }
        if let Some(best) = task.best_metric {
            suffix.push_str(&format!(" · best {best}"));
        }
        let mut spans = vec![
            Span::styled(
                format!("{marker}{indent}{} ", task.glyph()),
                theme::status_style(&task.status),
            ),
            Span::styled(
                format!("{:<9}", theme::clip(&task.id, 9)),
                theme::bold(theme::TEXT),
            ),
            Span::styled(
                theme::clip_ellipsis(&task.title, 40),
                if selected {
                    theme::bold(theme::TEXT)
                } else {
                    theme::fg(theme::TEXT)
                },
            ),
            Span::styled(suffix, theme::faint()),
        ];
        if let Some(target) = &task.metric_target {
            spans.push(Span::styled(
                format!("  [{target}]"),
                theme::fg(theme::PURPLE),
            ));
        }
        let mut line = Line::from(spans);
        if selected {
            line = line.style(ratatui::style::Style::default().bg(theme::PANEL_BG));
        }
        lines.push(line);
    }

    frame.render_widget(
        Paragraph::new(lines).style(theme::base()),
        chunks[0],
    );

    if detail_height > 0 {
        draw_detail(frame, app, chunks[1]);
    }
}

fn draw_detail(frame: &mut Frame, app: &App, area: Rect) {
    let Some(task) = app.selected_task() else {
        return;
    };
    let mut lines: Vec<Line<'static>> = Vec::new();
    lines.push(Line::from(vec![
        Span::styled(format!("{} ", task.id), theme::bold(theme::CYAN)),
        Span::styled(
            format!("[{}/{}] {}", task.kind, if task.mode.is_empty() { "AUTONOMOUS" } else { &task.mode }, task.status),
            theme::status_style(&task.status),
        ),
        Span::styled(format!("  {}", task.title), theme::fg(theme::TEXT)),
    ]));

    let width = area.width.saturating_sub(2) as usize;
    for chunk in wrap_text(&task.spec, width).into_iter().take(2) {
        lines.push(Line::from(Span::styled(chunk, theme::dim())));
    }
    if let Some(cmd) = &task.verify_cmd {
        lines.push(Line::from(vec![
            Span::styled("verify ", theme::faint()),
            Span::styled(theme::clip_ellipsis(cmd, width.saturating_sub(8)), theme::fg(theme::BLUE)),
        ]));
    }

    // Metric convergence sparkline for quantitative goals.
    let metrics: Vec<f64> = app
        .snapshot
        .task_attempts(&task.id)
        .into_iter()
        .filter_map(|a| a.metric_value)
        .collect();
    if metrics.len() >= 2 {
        let max = metrics.iter().cloned().fold(f64::MIN, f64::max);
        let min = metrics.iter().cloned().fold(f64::MAX, f64::min);
        let span = (max - min).abs().max(f64::EPSILON);
        let data: Vec<u64> = metrics
            .iter()
            .map(|v| (((v - min) / span) * 100.0) as u64)
            .collect();
        lines.push(Line::from(Span::styled(
            format!(
                "metric {} → {} over {} attempts",
                min,
                max,
                metrics.len()
            ),
            theme::fg(theme::PURPLE),
        )));
        let spark_area = Rect {
            x: area.x,
            y: area.y + area.height.saturating_sub(1),
            width: area.width,
            height: 1,
        };
        frame.render_widget(
            Sparkline::default()
                .data(&data)
                .style(theme::fg(theme::ACCENT)),
            spark_area,
        );
    }

    frame.render_widget(
        Paragraph::new(lines).style(theme::base()),
        Rect {
            height: area.height.saturating_sub(1).max(1),
            ..area
        },
    );
}

fn scroll_offset(selected: usize, len: usize, height: usize) -> usize {
    if len <= height || height == 0 {
        return 0;
    }
    if selected < height {
        0
    } else {
        (selected + 1).saturating_sub(height)
    }
}

/// Renders the task detail modal.
pub fn task_modal_lines(app: &App, task_id: &str) -> Vec<Line<'static>> {
    let Some(task) = app.snapshot.task(task_id) else {
        return vec![Line::from(Span::styled("task not found", theme::dim()))];
    };
    let mut lines = Vec::new();
    lines.push(Line::from(vec![
        Span::styled(format!("{} ", task.id), theme::bold(theme::CYAN)),
        Span::styled(task.title.clone(), theme::bold(theme::TEXT)),
    ]));
    lines.push(Line::from(vec![
        Span::styled(
            format!(
                "{} · {} · {} · ",
                task.kind,
                if task.mode.is_empty() { "AUTONOMOUS" } else { &task.mode },
                task.status
            ),
            theme::status_style(&task.status),
        ),
        Span::styled(format!("repo={} · priority={}", task.repo, task.priority), theme::dim()),
    ]));
    if let Some(parent) = &task.parent_id {
        lines.push(Line::from(Span::styled(format!("parent {parent}"), theme::dim())));
    }
    lines.push(Line::from(""));
    for chunk in wrap_text(&task.spec, 74) {
        lines.push(Line::from(Span::styled(chunk, theme::fg(theme::TEXT))));
    }
    lines.push(Line::from(""));
    if let Some(target) = &task.metric_target {
        lines.push(Line::from(vec![
            Span::styled("metric_target  ", theme::dim()),
            Span::styled(target.clone(), theme::fg(theme::PURPLE)),
        ]));
    }
    if let Some(cmd) = &task.verify_cmd {
        lines.push(Line::from(vec![
            Span::styled("verify_cmd     ", theme::dim()),
            Span::styled(cmd.clone(), theme::fg(theme::BLUE)),
        ]));
    }
    if let Some(best) = task.best_metric {
        lines.push(Line::from(vec![
            Span::styled("best_metric    ", theme::dim()),
            Span::styled(best.to_string(), theme::fg(theme::ACCENT)),
        ]));
    }
    if let Some(branch) = &task.best_branch {
        lines.push(Line::from(vec![
            Span::styled("best_branch    ", theme::dim()),
            Span::styled(branch.clone(), theme::fg(theme::BLUE)),
        ]));
    }
    if let Some(sha) = &task.best_sha {
        lines.push(Line::from(vec![
            Span::styled("best_sha       ", theme::dim()),
            Span::styled(theme::clip(sha, 12), theme::dim()),
        ]));
    }
    if let Some(lease) = &task.lease_until {
        lines.push(Line::from(vec![
            Span::styled("lease_until    ", theme::dim()),
            Span::styled(lease.clone(), theme::fg(theme::WARN)),
        ]));
    }
    lines.push(Line::from(vec![
        Span::styled("created        ", theme::dim()),
        Span::styled(task.created_at.clone(), theme::faint()),
        Span::styled("   updated ", theme::dim()),
        Span::styled(task.updated_at.clone(), theme::faint()),
    ]));
    lines.push(Line::from(""));

    let attempts = app.snapshot.task_attempts(task_id);
    lines.push(Line::from(Span::styled(
        format!("ATTEMPTS ({})", attempts.len()),
        theme::bold(theme::PURPLE),
    )));
    for attempt in attempts.iter().rev().take(8) {
        lines.push(Line::from(vec![
            Span::styled(format!("#{:<4}", attempt.id), theme::faint()),
            Span::styled(format!("{:<8}", attempt.role), theme::fg(theme::PURPLE)),
            Span::styled(format!("{:<16}", attempt.outcome), theme::outcome_style(&attempt.outcome)),
            Span::styled(format!("${:.4}", attempt.cost_usd), theme::fg(theme::WARN)),
            if let Some(m) = attempt.metric_value {
                Span::styled(format!("  metric={m}"), theme::fg(theme::ACCENT))
            } else {
                Span::styled(String::new(), theme::dim())
            },
        ]));
    }
    lines
}

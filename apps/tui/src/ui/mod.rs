//! Root layout: header bar, 4-quadrant cockpit, footer keyhints, and modals.

pub mod grid;
pub mod ledger;
pub mod prompts;
pub mod tasks;
pub mod theme;
pub mod topology;

use ratatui::layout::{Alignment, Constraint, Layout, Rect};
use ratatui::style::Style;
use ratatui::text::{Line, Span, Text};
use ratatui::widgets::{Block, Borders, Clear, Paragraph};
use ratatui::Frame;

use crate::app::{App, Modal, Pane};
use crate::model::PendingPair;

pub fn draw(frame: &mut Frame, app: &mut App) {
    let area = frame.area();
    frame.render_widget(Block::default().style(Style::default().bg(theme::BG)), area);
    if area.width < 40 || area.height < 12 {
        frame.render_widget(
            Paragraph::new("terminal too small — resize or press q").style(theme::base()),
            area,
        );
        return;
    }

    let ledger_height = if app.zoomed == Some(Pane::Ledger) {
        0
    } else if area.height >= 40 {
        14
    } else if area.height >= 30 {
        11
    } else {
        8
    };

    let chunks = Layout::vertical([
        Constraint::Length(3),
        Constraint::Min(3),
        Constraint::Length(ledger_height),
        Constraint::Length(1),
    ])
    .split(area);

    draw_header(frame, app, chunks[0]);

    if let Some(pane) = app.zoomed {
        match pane {
            Pane::Topology => topology::draw(frame, app, chunks[1]),
            Pane::Prompts => prompts::draw(frame, app, chunks[1]),
            Pane::Tasks => tasks::draw(frame, app, chunks[1]),
            Pane::Ledger => {
                let full = Rect {
                    y: chunks[1].y,
                    height: chunks[1].height + chunks[2].height,
                    ..chunks[1]
                };
                ledger::draw(frame, app, full);
            }
        }
    } else {
        let columns =
            Layout::horizontal([Constraint::Percentage(42), Constraint::Percentage(58)])
                .split(chunks[1]);
        let left =
            Layout::vertical([Constraint::Percentage(58), Constraint::Percentage(42)])
                .split(columns[0]);
        topology::draw(frame, app, left[0]);
        tasks::draw(frame, app, left[1]);
        prompts::draw(frame, app, columns[1]);
        ledger::draw(frame, app, chunks[2]);
    }

    // A pending pairing code sits over the panes: it is the one thing to read, and it is gone once it is used.
    if let (Some(pair), Some(left)) = (app.pending_pair.clone(), app.pair_seconds_left()) {
        draw_pair_banner(frame, chunks[1], &pair, left);
    }

    draw_footer(frame, app, chunks[3]);

    if let Some(modal) = app.modal.clone() {
        draw_modal(frame, app, area, modal);
    }
}

fn draw_header(frame: &mut Frame, app: &App, area: Rect) {
    let block = Block::default()
        .borders(Borders::BOTTOM)
        .border_style(theme::fg(theme::BORDER))
        .style(Style::default().bg(theme::BG));
    let inner = block.inner(area);
    frame.render_widget(block, area);

    let mode_style = if app.snapshot.state.mode == "AUTONOMOUS" {
        theme::bold(theme::ACCENT)
    } else {
        theme::bold(theme::CYAN)
    };
    let daemon = if app.daemon_connected {
        Span::styled("● live", theme::bold(theme::ACCENT))
    } else {
        Span::styled("○ offline", theme::bold(theme::DANGER))
    };
    let phase_style = if app.is_running() {
        theme::bold(theme::WARN)
    } else {
        theme::dim()
    };

    let mut row0 = vec![
        Span::styled(" BRAIN ", theme::bold(theme::PURPLE)),
        Span::styled(format!("[{}] ", app.snapshot.state.mode), mode_style),
        Span::styled(format!("{}:{} ", app.cfg.host, app.cfg.port), theme::faint()),
        daemon,
        Span::styled("  phase ", theme::faint()),
        Span::styled(app.phase.clone(), phase_style),
    ];
    if let (Some(pair), Some(left)) = (&app.pending_pair, app.pair_seconds_left()) {
        row0.push(Span::styled(
            format!("   [PAIR {} · {}s] ", pair.spaced(), left),
            theme::bold(theme::WARN),
        ));
    }
    if let Some(task) = &app.phase_task {
        row0.push(Span::styled(format!(" · {task}"), theme::fg(theme::CYAN)));
    }
    if let Some(role) = &app.phase_role {
        row0.push(Span::styled(format!(" · {role}"), theme::fg(theme::PURPLE)));
    }
    if let Some(run) = &app.active_run {
        row0.push(Span::styled(format!("  run {run}"), theme::faint()));
    }
    if app.live_cost > 0.0 {
        row0.push(Span::styled(
            format!("  ${:.4} / {:.0}s", app.live_cost, app.live_elapsed),
            theme::fg(theme::WARN),
        ));
    }

    let state = &app.snapshot.state;
    let ratio = state.budget_ratio();
    let bar_width = 28usize;
    let bar = budget_bar(ratio, bar_width);
    let bar_style = if ratio >= 0.95 {
        theme::fg(theme::DANGER)
    } else if ratio >= 0.75 {
        theme::fg(theme::WARN)
    } else {
        theme::fg(theme::ACCENT)
    };
    let mut row1 = vec![
        Span::styled(" budget ", theme::faint()),
        Span::styled(bar, bar_style),
        Span::styled(
            format!(
                " ${:.2} / ${:.2} ({:.0}%)",
                state.spent_today_usd,
                state.daily_budget_usd,
                ratio * 100.0
            ),
            theme::fg(theme::TEXT),
        ),
        Span::styled(
            format!("  remaining ${:.2}", state.remaining_usd()),
            theme::faint(),
        ),
    ];
    if let Some(sha) = &state.last_good_sha {
        row1.push(Span::styled(
            format!(
                "  probation {} run(s) · {} fail · sha {}",
                state.upgrade_runs,
                state.upgrade_failures,
                theme::clip(sha, 8)
            ),
            theme::fg(theme::WARN),
        ));
    }
    if !app.cfg.has_api_key() {
        row1.push(Span::styled(
            "   [no BRAIN_API_KEY — live telemetry disabled]",
            theme::bold(theme::DANGER),
        ));
    }
    let approvals = app.snapshot.counts.approvals_queued;
    if approvals > 0 {
        row1.push(Span::styled(
            format!("   [! {approvals} APPROVAL QUEUED]"),
            theme::bold(theme::DANGER),
        ));
    }

    let file = app
        .session_path
        .as_ref()
        .and_then(|p| p.file_name())
        .and_then(|n| n.to_str())
        .unwrap_or("—");
    let last_event = app
        .events
        .last()
        .map(|e| e.event.clone())
        .unwrap_or_else(|| "—".to_string());
    let row2 = vec![
        Span::styled(" session ", theme::faint()),
        Span::styled(theme::clip_ellipsis(file, 30), theme::dim()),
        Span::styled(format!(" · {} turns", app.turns.len()), theme::faint()),
        Span::styled(
            format!(" · follow {}", if app.follow { "on" } else { "off" }),
            if app.follow {
                theme::fg(theme::ACCENT)
            } else {
                theme::faint()
            },
        ),
        Span::styled(
            format!(
                "   events {} · last {}   mode {} · skills {}/{}",
                app.events.len(),
                last_event,
                app.snapshot.state.spend_date,
                app.snapshot.counts.skills_active,
                app.snapshot.counts.skills_total
            ),
            theme::faint(),
        ),
    ];

    frame.render_widget(
        Paragraph::new(Text::from(vec![
            Line::from(row0),
            Line::from(row1),
            Line::from(row2),
        ]))
        .style(theme::base()),
        inner,
    );
}

fn budget_bar(ratio: f64, width: usize) -> String {
    let filled = ((ratio * width as f64).round() as usize).min(width);
    format!("{}{}", "█".repeat(filled), "░".repeat(width - filled))
}

fn draw_footer(frame: &mut Frame, app: &App, area: Rect) {
    if let Some(buffer) = &app.command {
        let line = Line::from(vec![
            Span::styled(" : ", theme::bold(theme::CYAN)),
            Span::styled(buffer.clone(), theme::fg(theme::TEXT)),
            Span::styled("▌", theme::bold(theme::CYAN)),
            Span::styled("   Enter send · Esc cancel", theme::faint()),
        ]);
        frame.render_widget(Paragraph::new(line).style(theme::base()), area);
        return;
    }

    let mut spans = vec![
        Span::styled(format!(" [{}] ", app.focus.label()), theme::fg(theme::PURPLE)),
        Span::styled("q", theme::bold(theme::CYAN)),
        Span::styled(" quit ", theme::faint()),
        Span::styled("?", theme::bold(theme::CYAN)),
        Span::styled(" help ", theme::faint()),
        Span::styled("Tab/1-4", theme::bold(theme::CYAN)),
        Span::styled(" focus ", theme::faint()),
        Span::styled("z", theme::bold(theme::CYAN)),
        Span::styled(" zoom ", theme::faint()),
        Span::styled("j/k", theme::bold(theme::CYAN)),
        Span::styled(" move ", theme::faint()),
        Span::styled("Enter", theme::bold(theme::CYAN)),
        Span::styled(" inspect ", theme::faint()),
        Span::styled("a/r", theme::bold(theme::CYAN)),
        Span::styled(" approve ", theme::faint()),
        Span::styled("m", theme::bold(theme::CYAN)),
        Span::styled(" mode ", theme::faint()),
        Span::styled("x", theme::bold(theme::CYAN)),
        Span::styled(" stop ", theme::faint()),
        Span::styled(":", theme::bold(theme::CYAN)),
        Span::styled(" ask ", theme::faint()),
        Span::styled("y", theme::bold(theme::CYAN)),
        Span::styled(" copy", theme::faint()),
    ];
    if app.toast_active() {
        spans.push(Span::styled("   ▸ ", theme::fg(theme::PURPLE)));
        spans.push(Span::styled(
            app.toast_text().unwrap_or_default().to_string(),
            theme::bold(theme::WARN),
        ));
    }
    frame.render_widget(Paragraph::new(Line::from(spans)).style(theme::base()), area);
}

fn centered(percent_x: u16, height: u16, area: Rect) -> Rect {
    let height = height.min(area.height.saturating_sub(2)).max(5);
    let vertical = Layout::vertical([
        Constraint::Length(area.height.saturating_sub(height) / 2),
        Constraint::Length(height),
        Constraint::Min(0),
    ])
    .split(area);
    Layout::horizontal([
        Constraint::Percentage((100 - percent_x) / 2),
        Constraint::Percentage(percent_x),
        Constraint::Percentage((100 - percent_x) / 2),
    ])
    .split(vertical[1])[1]
}

fn draw_pair_banner(frame: &mut Frame, area: Rect, pair: &PendingPair, seconds_left: i64) {
    let lines = vec![
        Line::from(Span::styled(
            format!("{} · {}:{:02} left", pair.name, seconds_left / 60, seconds_left % 60),
            theme::fg(theme::TEXT),
        )),
        Line::from(""),
        Line::from(Span::styled(pair.spaced(), theme::bold(theme::CYAN))),
        Line::from(""),
        Line::from(Span::styled("type it into the Jarvis app", theme::faint())),
    ];
    let rect = centered_fixed(40, lines.len() as u16 + 2, area);
    frame.render_widget(Clear, rect);
    let block = Block::default()
        .borders(Borders::ALL)
        .border_style(theme::bold(theme::WARN))
        .title(Line::from(Span::styled(
            " pairing ",
            theme::bold(theme::WARN),
        )))
        .style(Style::default().bg(theme::BG));
    frame.render_widget(
        Paragraph::new(Text::from(lines))
            .block(block)
            .alignment(Alignment::Center),
        rect,
    );
}

fn centered_fixed(width: u16, height: u16, area: Rect) -> Rect {
    let width = width.min(area.width.saturating_sub(4)).max(16);
    let height = height.min(area.height.saturating_sub(2)).max(5);
    Rect {
        x: area.x + area.width.saturating_sub(width) / 2,
        y: area.y + area.height.saturating_sub(height) / 2,
        width,
        height,
    }
}

fn draw_modal(frame: &mut Frame, app: &App, area: Rect, modal: Modal) {
    let (title, lines) = match &modal {
        Modal::Help => ("KEYBINDINGS", help_lines()),
        Modal::Node(index) => {
            let spec = topology::node_spec(*index);
            (spec.title, topology::node_modal_lines(app, *index))
        }
        Modal::Attempt(id) => ("ATTEMPT DETAIL", prompts::attempt_lines(app, *id)),
        Modal::Skill(slug) => ("SKILL DETAIL", ledger::skill_modal_lines(app, slug)),
        Modal::Task(id) => ("TASK DETAIL", tasks::task_modal_lines(app, id)),
    };

    let desired = (lines.len() as u16).saturating_add(2);
    let rect = centered(78, desired, area);
    frame.render_widget(Clear, rect);
    let block = Block::default()
        .borders(Borders::ALL)
        .border_style(theme::bold(theme::BORDER_FOCUS))
        .title(Line::from(Span::styled(
            format!(" {title} "),
            theme::bold(theme::CYAN),
        )))
        .style(Style::default().bg(theme::BG));
    let inner = block.inner(rect);
    frame.render_widget(block, rect);

    let height = inner.height as usize;
    let offset = app.scroll_ledger.min(lines.len().saturating_sub(1));
    let visible: Vec<Line<'static>> = lines.into_iter().skip(offset).take(height).collect();
    frame.render_widget(
        Paragraph::new(Text::from(visible))
            .style(theme::base())
            .alignment(Alignment::Left),
        inner,
    );
}

fn help_lines() -> Vec<Line<'static>> {
    let rows = [
        ("q / Ctrl-c", "quit"),
        ("? ", "toggle this help"),
        ("Tab / 1-4", "focus Architecture · Prompts · Tasks · Ledger"),
        ("z", "zoom the focused pane fullscreen"),
        ("j / k, g / G", "move selection, jump to first/last"),
        ("PageUp / PageDown", "scroll the focused pane or modal"),
        ("[ / ]", "switch ledger tabs (Attempts · Skills · Outbox · Notes · DB)"),
        ("f", "toggle live follow on the prompt stream"),
        ("Enter", "inspect node / expand prompt / open skill or task detail"),
        ("a / r", "approve or reject the selected queued outbox approval"),
        ("m", "toggle ASSIST ↔ AUTONOMOUS via /v1/ask"),
        ("x", "stop the active run"),
        (":", "send a natural-language instruction to /v1/ask"),
        ("y", "copy the focused content to the macOS clipboard"),
        ("Esc", "close modal / leave zoom"),
    ];
    let mut lines = vec![Line::from("")];
    for (key, desc) in rows {
        lines.push(Line::from(vec![
            Span::styled(format!("  {key:<20}"), theme::bold(theme::CYAN)),
            Span::styled(desc.to_string(), theme::fg(theme::TEXT)),
        ]));
    }
    lines.push(Line::from(""));
    lines.push(Line::from(Span::styled(
        "  Reads ~/.config/jarvis/brain/state.db read-only; live events come from GET /v1/telemetry/events.",
        theme::faint(),
    )));
    lines
}


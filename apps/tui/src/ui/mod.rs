//! Root layout: left workspace (header, flow, payload, tasks, ledger, footer) + full-height right X-Ray sidebar.

pub mod grid;
pub mod ledger;
pub mod prompts;
pub mod tasks;
pub mod theme;
pub mod topology;
pub mod xray;

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
    if area.width < 60 || area.height < 18 {
        frame.render_widget(
            Paragraph::new("terminal too small — jarvis top wants a wide screen (resize or press q)")
                .style(theme::base()),
            area,
        );
        return;
    }

    // A big screen gets the whole flow map plus a full-height X-Ray/Logs sidebar on the far right.
    let flow_height = if area.height >= 44 { 12 } else { 10 };
    let bottom_height = if area.height >= 40 { 8 } else { 6 };

    if let Some(pane) = app.zoomed {
        let chunks = Layout::vertical([
            Constraint::Length(4),
            Constraint::Min(8),
            Constraint::Length(1),
        ])
        .split(area);
        draw_header(frame, app, chunks[0], true);
        match pane {
            Pane::Topology => topology::draw(frame, app, chunks[1]),
            Pane::Prompts => prompts::draw(frame, app, chunks[1]),
            Pane::Tasks => tasks::draw(frame, app, chunks[1]),
            Pane::Ledger => ledger::draw(frame, app, chunks[1]),
            Pane::Xray => xray::draw(frame, app, chunks[1]),
        }
        if let (Some(pair), Some(left)) = (app.pending_pair.clone(), app.pair_seconds_left()) {
            draw_pair_banner(frame, chunks[1], &pair, left);
        }
        draw_footer(frame, app, chunks[2]);
    } else {
        let sidebar_w = if area.width >= 100 {
            (area.width * 33 / 100)
                .clamp(38, 68)
                .min(area.width.saturating_sub(62))
        } else {
            (area.width / 3).max(24)
        };
        let columns = Layout::horizontal([Constraint::Min(36), Constraint::Length(sidebar_w)]).split(area);

        let left = Layout::vertical([
            Constraint::Length(4),
            Constraint::Length(flow_height),
            Constraint::Min(8),
            Constraint::Length(bottom_height),
            Constraint::Length(1),
        ])
        .split(columns[0]);

        draw_header(frame, app, left[0], false);
        topology::draw(frame, app, left[1]);
        prompts::draw(frame, app, left[2]);

        let bottom = Layout::horizontal([Constraint::Fill(4), Constraint::Fill(5)]).split(left[3]);
        tasks::draw(frame, app, bottom[0]);
        ledger::draw(frame, app, bottom[1]);

        if let (Some(pair), Some(left_s)) = (app.pending_pair.clone(), app.pair_seconds_left()) {
            draw_pair_banner(frame, left[2], &pair, left_s);
        }

        draw_footer(frame, app, left[4]);
        xray::draw(frame, app, columns[1]);
    }

    if let Some(modal) = app.modal.clone() {
        draw_modal(frame, app, area, modal);
    }
}

fn draw_header(frame: &mut Frame, app: &App, area: Rect, show_mic: bool) {
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
        Span::styled(" JARVIS ", theme::bold(theme::PURPLE)),
        Span::styled(format!("[{}] ", app.snapshot.state.mode), mode_style),
        Span::styled("voice ", theme::faint()),
        Span::styled(
            app.voice_mode.clone(),
            if app.voice_mode == "BEEP_ONLY" {
                theme::bold(theme::WARN)
            } else {
                theme::fg(theme::ACCENT)
            },
        ),
        Span::styled("  check ", theme::faint()),
        Span::styled(app.check_mode.clone(), theme::dim()),
        Span::styled("  ear ", theme::faint()),
        Span::styled(app.ear.clone(), theme::fg(theme::CYAN)),
        Span::styled("  speaker ", theme::faint()),
        Span::styled(
            app.speaker_label.clone(),
            if app.speaker_known {
                theme::bold(theme::ACCENT)
            } else {
                theme::fg(theme::WARN)
            },
        ),
        Span::styled(
            if app.speaker_confidence > 0.0 && !app.speaker_known {
                format!(" {:.0}%", app.speaker_confidence * 100.0)
            } else {
                String::new()
            },
            theme::faint(),
        ),
        Span::styled(format!("  {}:{} ", app.cfg.host, app.cfg.port), theme::faint()),
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

    let last_event = app
        .events
        .last()
        .map(|e| e.event.clone())
        .unwrap_or_else(|| "—".to_string());
    let mut row2 = vec![Span::styled(" turn ", theme::faint())];
    match app.newest_trace() {
        Some(trace) => {
            row2.push(Span::styled(format!("#{} ", trace.id), theme::bold(theme::CYAN)));
            row2.push(Span::styled(
                theme::clip_ellipsis(&trace.headline(), 40),
                theme::fg(theme::TEXT),
            ));
            // The same numbers the x-ray pane lists, one line: where the turn's time went.
            for kind in ["STT", "INTENT", "MEM", "PREP", "THINK", "LAT"] {
                if let Some(stage) = trace.stage(kind) {
                    if stage.detail.is_empty() {
                        continue;
                    }
                    row2.push(Span::styled(format!(" · {kind} "), theme::faint()));
                    row2.push(Span::styled(stage.detail.clone(), theme::dim()));
                }
            }
        }
        None => row2.push(Span::styled(
            "waiting for a sentence (the flow fills in as it happens)",
            theme::dim(),
        )),
    }
    row2.push(Span::styled(
        format!(
            "   events {} · last {} · skills {}/{} · {} msgs · follow {}",
            app.events.len(),
            last_event,
            app.snapshot.counts.skills_active,
            app.snapshot.counts.skills_total,
            app.turns.len(),
            if app.follow { "on" } else { "off" },
        ),
        theme::faint(),
    ));

    let rows = Layout::vertical([Constraint::Length(1), Constraint::Min(2)]).split(inner);
    if show_mic && inner.width >= 70 {
        let top = Layout::horizontal([Constraint::Min(10), Constraint::Length(24)]).split(rows[0]);
        frame.render_widget(Paragraph::new(Line::from(row0)).style(theme::base()), top[0]);
        frame.render_widget(
            Paragraph::new(mic_meter_line(app))
                .alignment(Alignment::Right)
                .style(theme::base()),
            top[1],
        );
    } else {
        frame.render_widget(Paragraph::new(Line::from(row0)).style(theme::base()), rows[0]);
    }
    frame.render_widget(
        Paragraph::new(Text::from(vec![Line::from(row1), Line::from(row2)])).style(theme::base()),
        rows[1],
    );
}

pub(crate) fn mic_meter_line(app: &App) -> Line<'static> {
    const CELLS: usize = 10;
    let filled = ((app.mic_ratio() * CELLS as f64).round() as usize).min(CELLS);
    let label = if app.mic_source == "relay" { " pair " } else { " mic " };
    let mut spans = vec![Span::styled(label, theme::faint())];
    for idx in 0..CELLS {
        if idx < filled {
            let color = if idx >= 8 {
                theme::DANGER
            } else if idx >= 6 {
                theme::WARN
            } else {
                theme::ACCENT
            };
            spans.push(Span::styled("█", theme::fg(color)));
        } else {
            spans.push(Span::styled("░", theme::faint()));
        }
    }
    let readout = match (app.live_mic_peak(), app.mic_db()) {
        (None, _) => Span::styled("   off ", theme::faint()),
        (Some(0.0), _) => Span::styled("  dead ", theme::fg(theme::DANGER)),
        (Some(_), None) => Span::styled(" -60dB ", theme::dim()),
        (Some(_), Some(db)) => Span::styled(format!(" {:>3.0}dB ", db), theme::fg(theme::TEXT)),
    };
    spans.push(readout);
    Line::from(spans)
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
        Span::styled("1-5", theme::bold(theme::CYAN)),
        Span::styled(" focus ", theme::faint()),
        Span::styled("z", theme::bold(theme::CYAN)),
        Span::styled(" zoom ", theme::faint()),
        Span::styled("j/k", theme::bold(theme::CYAN)),
        Span::styled(" move ", theme::faint()),
        Span::styled("Enter", theme::bold(theme::CYAN)),
        Span::styled(" inspect ", theme::faint()),
        Span::styled("p", theme::bold(theme::CYAN)),
        Span::styled(" payload ", theme::faint()),
        Span::styled("v/b/e/A", theme::bold(theme::CYAN)),
        Span::styled(" log filter ", theme::faint()),
        Span::styled("f", theme::bold(theme::CYAN)),
        Span::styled(" follow ", theme::faint()),
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
        ("Tab / 1-5", "focus Flow · Payload · Tasks · Ledger · X-Ray & Logs"),
        ("z", "zoom the focused pane fullscreen"),
        ("j / k, g / G", "move selection (turns, nodes, tasks), jump to first/last"),
        ("PageUp / PageDown", "scroll the focused pane or modal"),
        ("p", "cycle the payload tab: Voice Ctx · Runner · Retell"),
        ("v / b / e / A", "log filter: voice · brain · errors · all"),
        ("f", "toggle live follow on the newest turn"),
        ("Enter", "inspect node / cycle payload / open skill or task detail"),
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


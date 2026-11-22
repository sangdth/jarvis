//! Pane 1 — live architecture schematic: how every Superbrain component is wired together,
//! with activity pulses bound to live telemetry.

use ratatui::layout::Rect;
use ratatui::widgets::Paragraph;
use ratatui::Frame;

use crate::app::{App, Pane};
use crate::ui::grid::Canvas;
use crate::ui::theme;

const BW: usize = 22;
const BH: usize = 3;
const C1: usize = 0;
const C2: usize = 24;
const C3: usize = 50;

pub struct NodeSpec {
    pub id: &'static str,
    pub title: &'static str,
    pub summary: &'static str,
    pub detail: &'static str,
}

pub const NODES: [NodeSpec; 13] = [
    NodeSpec {
        id: "macbook",
        title: "MacBook · Jarvis",
        summary: "Voice harness → HermesAdapter",
        detail: "POSTs /v1/runs over Tailscale with a Bearer token. Each request becomes a RunRecord whose SSE events the voice harness speaks aloud.",
    },
    NodeSpec {
        id: "iphone",
        title: "iPhone · /v1/ask",
        summary: "Shortcut / Action Button",
        detail: "Synchronous JSON endpoint: sends {\"input\": ...} and speaks back the returned \"spoken\" field. No live approvals — deferred requests land in the outbox.",
    },
    NodeSpec {
        id: "server",
        title: "BrainServer",
        summary: "aiohttp · SSE · auth",
        detail: "Owns the Hermes 5-endpoint contract plus /v1/ask, /v1/telemetry/events and /v1/outbox/{id}/resolve. Every route is guarded by hmac-compared Bearer auth.",
    },
    NodeSpec {
        id: "db",
        title: "state.db",
        summary: "SQLite WAL · 5 tables",
        detail: "Single source of truth: system_state, tasks, attempts, skills, outbox. WAL mode lets this dashboard read concurrently while the daemon writes.",
    },
    NodeSpec {
        id: "reconciler",
        title: "Reconciler",
        summary: "Deterministic tick loop",
        detail: "Every reconcile_interval_seconds: reap stale leases, claim the highest-priority ready task for a free repo, spawn one ephemeral pi worker, then verify and push.",
    },
    NodeSpec {
        id: "planner",
        title: "Planner",
        summary: "Decompose · research",
        detail: "Read-only tools plus upsert_task. Decomposes goals into atomic children or fans out competing hypotheses during auto-research. Cannot edit files.",
    },
    NodeSpec {
        id: "builder",
        title: "Builder",
        summary: "edit_and_run · verify",
        detail: "Runs inside a 0700 worktree under sandbox-exec. May edit/write, but every path is realpath-checked and test files are protected on problem tasks.",
    },
    NodeSpec {
        id: "scout",
        title: "Scout",
        summary: "Byte-verified evidence",
        detail: "Seatbelt-sandboxed read-only worker (read,grep,find,ls). Every returned quote is re-verified byte-for-byte on disk before the planner may cite it.",
    },
    NodeSpec {
        id: "improver",
        title: "Improver",
        summary: "Skill reflection",
        detail: "Offline reflection over unreflected attempt traces. Proposes at most one create/append/update/retire per run; activation is gated by sha256 + evidence IDs.",
    },
    NodeSpec {
        id: "worktree",
        title: "Worktree",
        summary: "0700 · origin/master",
        detail: "sessions/worktrees/<task_id>: created fresh from origin/master before each build, removed after verification. Writes outside it are denied by sandbox-exec.",
    },
    NodeSpec {
        id: "verify",
        title: "verify_cmd",
        summary: "Deterministic gate",
        detail: "Runs outside pi with a hard timeout. For repo=superbrain it also runs self_upgrade_verify_cmd plus the sealed held-out baseline suite.",
    },
    NodeSpec {
        id: "github",
        title: "GitHub",
        summary: "origin/brain/<id>",
        detail: "Verified work is pushed to origin/brain/<task_id> and never auto-merged into master. GitHub is the handoff boundary between MacBook and Mac mini.",
    },
    NodeSpec {
        id: "skills",
        title: "Skills vault",
        summary: "SKILL.md · capped",
        detail: "~/.config/superbrain/skills/<slug>/SKILL.md. Capped at 10 active personal skills; two consecutive failures after an update roll a skill back.",
    },
];

pub fn node_spec(index: usize) -> &'static NodeSpec {
    &NODES[index.min(NODES.len() - 1)]
}

pub fn node_text(index: usize) -> String {
    let spec = node_spec(index);
    format!("{}\n{}\n\n{}", spec.title, spec.summary, spec.detail)
}

pub fn draw(frame: &mut Frame, app: &App, area: Rect) {
    let focused = app.focus == Pane::Topology;
    let block = theme::panel(focused, "ARCHITECTURE · LIVE TOPOLOGY");
    let inner = block.inner(area);
    frame.render_widget(block, area);
    if inner.width < 20 || inner.height < 5 {
        return;
    }

    let mut canvas = Canvas::new(inner.width as usize, inner.height as usize);

    // Vertical chain: clients → server → db → reconciler.
    draw_node(&mut canvas, C1, 0, app, 0);
    draw_node(&mut canvas, C1, 4, app, 1);
    draw_node(&mut canvas, C2, 2, app, 2);
    draw_node(&mut canvas, C2, 6, app, 3);
    draw_node(&mut canvas, C2, 10, app, 4);

    // Worker fan-out.
    draw_node(&mut canvas, C3, 0, app, 5);
    draw_node(&mut canvas, C3, 4, app, 6);
    draw_node(&mut canvas, C3, 8, app, 7);
    draw_node(&mut canvas, C3, 12, app, 8);

    let wire = theme::fg(theme::BORDER);
    let live_wire = theme::fg(theme::ACCENT);

    // Clients merge into BrainServer at (C2, 3).
    canvas.put(22, 1, '┐', wire);
    canvas.put(22, 2, '│', wire);
    canvas.put(22, 3, '├', wire);
    canvas.put(22, 4, '│', wire);
    canvas.put(22, 5, '┘', wire);
    canvas.put(
        23,
        3,
        '▶',
        if app.node_active("server") {
            live_wire
        } else {
            wire
        },
    );

    // server → db → reconciler.
    canvas.put(35, 5, '▼', wire);
    canvas.put(35, 9, '▼', wire);

    // reconciler → worker bus.
    let bus_active = app.is_running();
    canvas.put(46, 11, '├', if bus_active { live_wire } else { wire });
    canvas.vline(46, 1, 13, '│', if bus_active { live_wire } else { wire });
    canvas.put(46, 1, '┌', wire);
    canvas.put(46, 5, '├', wire);
    canvas.put(46, 9, '├', wire);
    canvas.put(46, 13, '└', wire);
    for (y, node) in [(1usize, "planner"), (5, "builder"), (9, "scout"), (13, "improver")] {
        canvas.hline(47, 48, y, '─', wire);
        canvas.put(
            49,
            y,
            '▶',
            if app.node_active(node) {
                live_wire
            } else {
                wire
            },
        );
    }

    // Deployment pipeline strip.
    let strip_y = 16;
    let selected_stage = app.sel_topology;
    let chip_style = |idx: usize, node: &str| -> ratatui::style::Style {
        if selected_stage == idx && app.focus == Pane::Topology {
            theme::bold(theme::CYAN)
        } else if app.node_active(node) {
            theme::bold(theme::ACCENT)
        } else {
            theme::dim()
        }
    };
    let mut x = 0usize;
    x = draw_text(&mut canvas, x, strip_y, "PIPELINE ", theme::bold(theme::PURPLE));
    x = draw_text(&mut canvas, x, strip_y, "board.ts ", chip_style(9, "board"));
    x = draw_text(&mut canvas, x, strip_y, "──▶ ", theme::faint());
    x = draw_text(&mut canvas, x, strip_y, "worktree(0700) ", chip_style(9, "worktree"));
    x = draw_text(&mut canvas, x, strip_y, "──▶ ", theme::faint());
    x = draw_text(&mut canvas, x, strip_y, "verify_cmd ", chip_style(10, "verify"));
    x = draw_text(&mut canvas, x, strip_y, "──▶ ", theme::faint());
    x = draw_text(&mut canvas, x, strip_y, "held-out ", chip_style(10, "verify"));
    x = draw_text(&mut canvas, x, strip_y, "──▶ ", theme::faint());
    draw_text(&mut canvas, x, strip_y, "origin/brain/<id>", chip_style(11, "github"));

    // Runtime strip.
    let skill_count = app.snapshot.skills.iter().filter(|s| s.status == "active").count();
    let loads: i64 = app
        .snapshot
        .skills
        .iter()
        .filter(|s| s.status == "active")
        .map(|s| s.loads_since_update)
        .sum();
    let mut x = 0usize;
    x = draw_text(&mut canvas, x, strip_y + 1, "SKILLS ", theme::bold(theme::PURPLE));
    x = draw_text(
        &mut canvas,
        x,
        strip_y + 1,
        &format!("{skill_count}/10 active · {loads} loads "),
        chip_style(12, "skills"),
    );
    x = draw_text(&mut canvas, x, strip_y + 1, "EVENTS ", theme::bold(theme::PURPLE));
    x = draw_text(
        &mut canvas,
        x,
        strip_y + 1,
        &format!("{} ", app.events.len()),
        theme::fg(theme::TEXT),
    );
    let (label, style) = if app.daemon_connected {
        ("SSE ● live", theme::bold(theme::ACCENT))
    } else {
        ("SSE ○ offline", theme::bold(theme::DANGER))
    };
    draw_text(&mut canvas, x, strip_y + 1, label, style);

    frame.render_widget(
        Paragraph::new(canvas.into_text()).style(theme::base()),
        inner,
    );
}

fn draw_text(canvas: &mut Canvas, x: usize, y: usize, text: &str, style: ratatui::style::Style) -> usize {
    canvas.text(x, y, text, style);
    x + text.chars().count()
}

fn draw_node(canvas: &mut Canvas, x: usize, y: usize, app: &App, idx: usize) {
    let spec = node_spec(idx);
    let active = app.node_active(spec.id);
    let selected = app.sel_topology == idx && app.focus == Pane::Topology;
    let border = if selected {
        theme::bold(theme::CYAN)
    } else if active {
        theme::fg(theme::ACCENT)
    } else {
        theme::fg(theme::BORDER)
    };
    canvas.rect(x, y, BW, BH, border, active);

    let inner_w = BW - 2;
    let left = format!(" {} ", spec.title);
    let right = subtitle(app, spec.id);
    let right_w = right.chars().count();
    let left_w = inner_w.saturating_sub(right_w + 2);
    let left_text = theme::clip(&left, left_w);
    let pad = inner_w.saturating_sub(left_text.chars().count() + right_w + 1);
    let line = format!("{left_text}{}{right}", " ".repeat(pad));
    let text_style = if active {
        theme::bold(theme::TEXT)
    } else {
        theme::fg(theme::TEXT)
    };
    canvas.text(x + 1, y + 1, &line, text_style);
    canvas.put(
        x + BW - 2,
        y + 1,
        if active { '●' } else { '○' },
        if active {
            theme::fg(theme::ACCENT)
        } else {
            theme::faint()
        },
    );
}

fn subtitle(app: &App, id: &str) -> String {
    match id {
        "server" => format!(":{}", app.cfg.port),
        "db" => {
            if app.snapshot.wal_bytes > 0 {
                format!("{} wal", theme::bytes_human(app.snapshot.wal_bytes))
            } else {
                format!("{} pages", app.snapshot.page_count)
            }
        }
        "reconciler" => short_phase(&app.phase),
        "planner" | "builder" | "improver" => {
            if app.phase_role.as_deref() == Some(id) {
                app.phase_task.clone().unwrap_or_default()
            } else {
                String::new()
            }
        }
        "scout" => {
            if app.pulse_level("scout") > 0.0 {
                "verifying".to_string()
            } else {
                String::new()
            }
        }
        _ => String::new(),
    }
}

pub fn short_phase(phase: &str) -> String {
    match phase {
        "assist_idle" => "assist".to_string(),
        "budget_paused" => "budget".to_string(),
        "pi_running" => "pi".to_string(),
        "pi_finished" => "done".to_string(),
        "skill_improver" => "reflect".to_string(),
        "auto_research" => "research".to_string(),
        other => theme::clip(other, 8),
    }
}

/// Renders the "Architecture X-Ray" modal body for the selected node.
pub fn node_modal_lines(app: &App, index: usize) -> Vec<ratatui::text::Line<'static>> {
    use ratatui::style::Style;
    use ratatui::text::{Line, Span};

    fn fact(lines: &mut Vec<Line<'static>>, label: &str, value: String, style: Style) {
        lines.push(Line::from(vec![
            Span::styled(format!("{label:<18}"), theme::dim()),
            Span::styled(value, style),
        ]));
    }

    let spec = node_spec(index);
    let mut lines = vec![
        Line::from(Span::styled(
            spec.summary.to_string(),
            theme::bold(theme::ACCENT),
        )),
        Line::from(""),
    ];
    for chunk in crate::ui::grid::wrap_text(spec.detail, 74) {
        lines.push(Line::from(Span::styled(chunk, theme::fg(theme::TEXT))));
    }
    lines.push(Line::from(""));

    match spec.id {
        "server" => {
            fact(&mut lines, "endpoint", app.cfg.endpoint(), theme::fg(theme::TEXT));
            fact(
                &mut lines,
                "daemon",
                if app.daemon_connected { "connected".into() } else { "offline".into() },
                if app.daemon_connected { theme::fg(theme::ACCENT) } else { theme::fg(theme::DANGER) },
            );
            if let Some(pid) = app.daemon_pid {
                fact(&mut lines, "pid", pid.to_string(), theme::fg(theme::TEXT));
            }
            fact(&mut lines, "uptime", format!("{:.0}s", app.daemon_uptime), theme::fg(theme::TEXT));
            fact(&mut lines, "mode", app.snapshot.state.mode.clone(), theme::fg(theme::CYAN));
        }
        "db" => {
            fact(&mut lines, "db size", theme::bytes_human(app.snapshot.db_bytes), theme::fg(theme::TEXT));
            fact(&mut lines, "wal", theme::bytes_human(app.snapshot.wal_bytes), theme::fg(theme::TEXT));
            fact(&mut lines, "shm", theme::bytes_human(app.snapshot.shm_bytes), theme::fg(theme::TEXT));
            fact(&mut lines, "pages", app.snapshot.page_count.to_string(), theme::fg(theme::TEXT));
            fact(&mut lines, "freelist", app.snapshot.freelist_count.to_string(), theme::fg(theme::TEXT));
            fact(&mut lines, "page size", app.snapshot.page_size.to_string(), theme::fg(theme::TEXT));
            fact(
                &mut lines,
                "rows",
                format!(
                    "{} tasks · {} attempts · {} skills · {} outbox",
                    app.snapshot.counts.tasks,
                    app.snapshot.counts.attempts,
                    app.snapshot.counts.skills_total,
                    app.snapshot.outbox.len()
                ),
                theme::fg(theme::TEXT),
            );
        }
        "reconciler" => {
            fact(&mut lines, "phase", app.phase.clone(), theme::fg(theme::ACCENT));
            fact(&mut lines, "task", app.phase_task.clone().unwrap_or_else(|| "—".into()), theme::fg(theme::TEXT));
            fact(&mut lines, "role", app.phase_role.clone().unwrap_or_else(|| "—".into()), theme::fg(theme::TEXT));
            fact(&mut lines, "live cost", format!("${:.4}", app.live_cost), theme::fg(theme::TEXT));
            fact(&mut lines, "elapsed", format!("{:.1}s", app.live_elapsed), theme::fg(theme::TEXT));
        }
        "planner" | "builder" | "scout" | "improver" => {
            let tools = match spec.id {
                "planner" => "read,grep,find,ls,research_scout,upsert_task,report_diagnostic,defer_for_approval",
                "builder" => "read,edit,write,edit_and_run,bash,grep,find,ls,research_scout,record_note,report_diagnostic,defer_for_approval",
                "scout" => "read,grep,find,ls",
                _ => "read,grep,find,ls,research_scout,propose_skill_change",
            };
            fact(&mut lines, "tools (-t)", String::new(), theme::fg(theme::TEXT));
            for chunk in crate::ui::grid::wrap_text(tools, 54) {
                lines.push(Line::from(Span::styled(format!("  {chunk}"), theme::fg(theme::BLUE))));
            }
            fact(
                &mut lines,
                "active",
                if app.node_active(spec.id) { "yes".into() } else { "no".into() },
                if app.node_active(spec.id) { theme::fg(theme::ACCENT) } else { theme::dim() },
            );
        }
        "skills" => {
            fact(&mut lines, "active", format!("{}/10", app.snapshot.counts.skills_active), theme::fg(theme::TEXT));
            fact(&mut lines, "total", app.snapshot.counts.skills_total.to_string(), theme::fg(theme::TEXT));
        }
        "macbook" | "iphone" => {
            fact(&mut lines, "transport", "Tailscale + Bearer".into(), theme::fg(theme::TEXT));
            fact(&mut lines, "endpoint", app.cfg.endpoint(), theme::fg(theme::TEXT));
        }
        "worktree" | "verify" | "github" => {
            fact(&mut lines, "stage", spec.summary.to_string(), theme::fg(theme::TEXT));
            fact(
                &mut lines,
                "active",
                if app.node_active(spec.id) { "yes".into() } else { "no".into() },
                if app.node_active(spec.id) { theme::fg(theme::ACCENT) } else { theme::dim() },
            );
        }
        _ => {}
    }
    lines
}

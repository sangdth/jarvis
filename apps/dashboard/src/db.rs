//! Read-only SQLite access. The dashboard never writes to `state.db`; it uses a separate
//! `SQLITE_OPEN_READ_ONLY` connection so it can never block the daemon's WAL writers.

use std::path::Path;
use std::time::Duration;

use rusqlite::{Connection, OpenFlags, Row};

use crate::config::Config;
use crate::model::{
    Attempt, Counts, DiskSpeaker, DiskSpeakers, Outbox, Skill, Snapshot, StageTone, SystemState, Task,
    Trace, TraceStage,
};

pub type DbResult<T> = Result<T, rusqlite::Error>;

pub fn open_read_only(path: &Path) -> DbResult<Connection> {
    let conn = Connection::open_with_flags(
        path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )?;
    conn.busy_timeout(Duration::from_millis(2500))?;
    Ok(conn)
}

fn file_len(path: &Path) -> u64 {
    std::fs::metadata(path).map(|m| m.len()).unwrap_or(0)
}

fn opt_str(row: &Row<'_>, name: &str) -> Option<String> {
    row.get::<_, Option<String>>(name).ok().flatten()
}

fn json_string_array(raw: Option<String>) -> Vec<String> {
    raw.and_then(|s| serde_json::from_str::<Vec<String>>(&s).ok())
        .unwrap_or_default()
}

fn json_i64_array(raw: Option<String>) -> Vec<i64> {
    raw.and_then(|s| serde_json::from_str::<Vec<i64>>(&s).ok())
        .unwrap_or_default()
}

pub fn load_snapshot(conn: &Connection, cfg: &Config) -> DbResult<Snapshot> {
    let mut snap = Snapshot::default();

    if let Ok(row) = conn.query_row(
        "SELECT mode, daily_budget_usd, spent_today_usd, spend_date, last_good_sha, \
         upgrade_runs, upgrade_failures FROM system_state WHERE id = 1",
        [],
        |row| {
            Ok(SystemState {
                mode: row.get::<_, String>("mode")?,
                daily_budget_usd: row.get::<_, f64>("daily_budget_usd")?,
                spent_today_usd: row.get::<_, f64>("spent_today_usd")?,
                spend_date: row.get::<_, String>("spend_date")?,
                last_good_sha: opt_str(row, "last_good_sha"),
                upgrade_runs: row.get::<_, i64>("upgrade_runs").unwrap_or(0),
                upgrade_failures: row.get::<_, i64>("upgrade_failures").unwrap_or(0),
            })
        },
    ) {
        snap.state = row;
        snap.has_state = true;
    }

    let mut stmt = conn.prepare(
        "SELECT * FROM tasks ORDER BY priority ASC, created_at ASC",
    )?;
    snap.tasks = stmt
        .query_map([], |row| {
            Ok(Task {
                id: row.get("id")?,
                parent_id: opt_str(row, "parent_id"),
                kind: row.get("kind")?,
                mode: row
                    .get::<_, Option<String>>("mode")
                    .ok()
                    .flatten()
                    .unwrap_or_else(|| "AUTONOMOUS".to_string()),
                repo: row.get("repo")?,
                title: row.get("title")?,
                spec: row.get("spec")?,
                metric_target: opt_str(row, "metric_target"),
                verify_cmd: opt_str(row, "verify_cmd"),
                status: row.get("status")?,
                priority: row.get::<_, i64>("priority").unwrap_or(50),
                strike_count: row.get::<_, i64>("strike_count").unwrap_or(0),
                child_strategy: row
                    .get::<_, Option<String>>("child_strategy")
                    .ok()
                    .flatten()
                    .unwrap_or_else(|| "all".to_string()),
                runner: row
                    .get::<_, Option<String>>("runner")
                    .ok()
                    .flatten()
                    .unwrap_or_else(|| "pi".to_string()),
                thread_id: opt_str(row, "thread_id"),
                best_metric: row.get::<_, Option<f64>>("best_metric").ok().flatten(),
                best_branch: opt_str(row, "best_branch"),
                best_sha: opt_str(row, "best_sha"),
                lease_until: opt_str(row, "lease_until"),
                created_at: row.get("created_at")?,
                updated_at: row.get("updated_at")?,
            })
        })?
        .filter_map(Result::ok)
        .collect();

    let mut stmt = conn.prepare(
        "SELECT id, task_id, role, pi_session_id, session_jsonl_path, thread_id, summary, git_branch, \
         git_sha, cost_usd, outcome, verify_output, metric_value, skills_used, diagnostic, \
         reflected, created_at FROM attempts ORDER BY id DESC LIMIT 400",
    )?;
    snap.attempts = stmt
        .query_map([], |row| {
            Ok(Attempt {
                id: row.get("id")?,
                task_id: row.get("task_id")?,
                role: row.get("role")?,
                pi_session_id: opt_str(row, "pi_session_id"),
                session_jsonl_path: opt_str(row, "session_jsonl_path"),
                summary: row.get("summary")?,
                git_branch: opt_str(row, "git_branch"),
                git_sha: opt_str(row, "git_sha"),
                cost_usd: row.get::<_, f64>("cost_usd").unwrap_or(0.0),
                outcome: row.get("outcome")?,
                verify_output: opt_str(row, "verify_output"),
                metric_value: row.get::<_, Option<f64>>("metric_value").ok().flatten(),
                skills_used: json_string_array(opt_str(row, "skills_used")),
                diagnostic: opt_str(row, "diagnostic"),
                reflected: row.get::<_, i64>("reflected").unwrap_or(0) != 0,
                thread_id: opt_str(row, "thread_id"),
                created_at: row.get("created_at")?,
            })
        })?
        .filter_map(Result::ok)
        .collect();

    let mut stmt = conn.prepare(
        "SELECT slug, name, description, version, sha256, status, content, previous_content, \
         lesson, evidence_attempt_ids, loads_since_update, successes_since_update, \
         failures_since_update, updated_at FROM skills ORDER BY status ASC, slug ASC",
    )?;
    snap.skills = stmt
        .query_map([], |row| {
            Ok(Skill {
                slug: row.get("slug")?,
                name: row.get("name")?,
                description: row.get("description")?,
                version: row.get::<_, i64>("version").unwrap_or(1),
                sha256: row.get("sha256")?,
                status: row.get("status")?,
                content: row.get("content")?,
                previous_content: opt_str(row, "previous_content"),
                lesson: row.get::<_, Option<String>>("lesson").ok().flatten().unwrap_or_default(),
                evidence_attempt_ids: json_i64_array(opt_str(row, "evidence_attempt_ids")),
                loads_since_update: row.get::<_, i64>("loads_since_update").unwrap_or(0),
                successes_since_update: row.get::<_, i64>("successes_since_update").unwrap_or(0),
                failures_since_update: row.get::<_, i64>("failures_since_update").unwrap_or(0),
                updated_at: row.get("updated_at")?,
            })
        })?
        .filter_map(Result::ok)
        .collect();

    let mut stmt = conn.prepare(
        "SELECT id, kind, task_id, summary_spoken, details_json, status, created_at \
         FROM outbox ORDER BY CASE WHEN kind = 'approval_request' AND status = 'queued' \
         THEN 0 ELSE 1 END ASC, created_at DESC LIMIT 200",
    )?;
    snap.outbox = stmt
        .query_map([], |row| {
            Ok(Outbox {
                id: row.get("id")?,
                kind: row.get("kind")?,
                task_id: opt_str(row, "task_id"),
                summary_spoken: row.get("summary_spoken")?,
                details_json: row
                    .get::<_, Option<String>>("details_json")
                    .ok()
                    .flatten()
                    .unwrap_or_else(|| "{}".to_string()),
                status: row.get("status")?,
                created_at: row.get("created_at")?,
            })
        })?
        .filter_map(Result::ok)
        .collect();

    snap.counts = Counts {
        tasks: count(conn, "tasks"),
        attempts: count(conn, "attempts"),
        skills_active: count_where(conn, "skills", "status = 'active'"),
        skills_total: count(conn, "skills"),
        outbox_queued: count_where(conn, "outbox", "status = 'queued'"),
        approvals_queued: count_where(
            conn,
            "outbox",
            "status = 'queued' AND kind = 'approval_request'",
        ),
    };

    snap.speakers = load_disk_speakers(cfg);
    snap.history = load_trace_history(conn);

    snap.db_bytes = file_len(&cfg.db_path);
    let wal_path = std::path::PathBuf::from(format!("{}-wal", cfg.db_path.display()));
    let shm_path = std::path::PathBuf::from(format!("{}-shm", cfg.db_path.display()));
    snap.wal_bytes = file_len(&wal_path);
    snap.shm_bytes = file_len(&shm_path);
    snap.page_count = pragma_i64(conn, "page_count");
    snap.freelist_count = pragma_i64(conn, "freelist_count");
    snap.page_size = pragma_i64(conn, "page_size");

    Ok(snap)
}

/// Recent turns and runs from the durable `trace_runs` table, so a late `jarvis dashboard` still shows what happened.
///
/// Only three stages are synthesized (STT, ROUTE, RETURN): the full stage list lives in the trace JSON file,
/// and reading 40 of those off the UI thread on every snapshot poll would cost more than it shows.
fn load_trace_history(conn: &Connection) -> Vec<Trace> {
    let mut stmt = match conn.prepare(
        "SELECT trace_id, outcome, friction_kind, surface, runner, user_input, output_summary, created_at \
         FROM trace_runs ORDER BY created_at DESC LIMIT 40",
    ) {
        Ok(stmt) => stmt,
        Err(_) => return Vec::new(), // an older state.db without the table: just no history yet
    };
    let rows = stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1).unwrap_or_default(),
            row.get::<_, Option<String>>(2).ok().flatten().unwrap_or_default(),
            row.get::<_, Option<String>>(3).ok().flatten().unwrap_or_default(),
            row.get::<_, Option<String>>(4).ok().flatten().unwrap_or_default(),
            row.get::<_, Option<String>>(5).ok().flatten().unwrap_or_default(),
            row.get::<_, Option<String>>(6).ok().flatten().unwrap_or_default(),
            row.get::<_, Option<String>>(7).ok().flatten().unwrap_or_default(),
        ))
    });
    let Ok(rows) = rows else { return Vec::new() };
    let mut history: Vec<Trace> = Vec::new();
    for (i, row) in rows.filter_map(Result::ok).enumerate() {
        let (id, outcome, friction, surface, runner, user_input, output_summary, created_at) = row;
        let ts = i as f64;
        let mut stages = vec![TraceStage::new("STT", StageTone::Info, ts, user_input, created_at)];
        if !surface.is_empty() {
            stages.push(TraceStage::new(
                "ROUTE",
                StageTone::Think,
                ts + 0.1,
                format!("{surface} · {runner}"),
                "",
            ));
        }
        let ok = outcome == "ok" && friction.is_empty();
        let detail = if friction.is_empty() {
            outcome.clone()
        } else {
            format!("{outcome}/{friction}")
        };
        stages.push(TraceStage::new(
            "RETURN",
            if ok { StageTone::Ok } else { StageTone::Bad },
            ts + 0.2,
            output_summary,
            detail,
        ));
        history.push(Trace {
            id,
            started: ts,
            stages,
            done: true,
        });
    }
    history.reverse(); // oldest first, matching the live append order
    history
}

/// Reads the saved speaker roster from `SOUL.md`, `owner_voice.npy`, and `speakers/speakers.json`.
pub fn load_disk_speakers(cfg: &Config) -> DiskSpeakers {
    let owner_name = std::fs::read_to_string(cfg.soul_path())
        .ok()
        .and_then(|text| parse_owner_name(&text));
    let owner_voiceprint = cfg.owner_voiceprint_path().is_file();
    let mut guests = Vec::new();
    if let Ok(raw) = std::fs::read_to_string(cfg.speakers_path()) {
        if let Ok(serde_json::Value::Object(map)) = serde_json::from_str::<serde_json::Value>(&raw) {
            for (id, meta) in map {
                let Some(obj) = meta.as_object() else {
                    continue;
                };
                let name = obj
                    .get("name")
                    .and_then(|v| v.as_str())
                    .map(str::trim)
                    .unwrap_or_default()
                    .to_string();
                if name.is_empty() {
                    continue;
                }
                guests.push(DiskSpeaker {
                    id,
                    name,
                    clips: obj.get("clips").and_then(|v| v.as_u64()).unwrap_or(1),
                    speech_secs: obj.get("speech_secs").and_then(|v| v.as_f64()).unwrap_or(0.0),
                    vouched: obj.get("vouched").and_then(|v| v.as_bool()).unwrap_or(false),
                });
            }
        }
    }
    guests.sort_by(|a, b| {
        a.name
            .to_ascii_lowercase()
            .cmp(&b.name.to_ascii_lowercase())
            .then_with(|| a.id.cmp(&b.id))
    });
    DiskSpeakers {
        owner_name,
        owner_voiceprint,
        guests,
    }
}

/// Mirrors `jarvis_core.soul._USER_NAME_RE` (`The user's name is "..."`).
pub fn parse_owner_name(text: &str) -> Option<String> {
    const MARKER: &str = "The user's name is \"";
    let start = text.find(MARKER)?;
    let rest = &text[start + MARKER.len()..];
    let end = rest.find('"')?;
    let name = rest[..end].trim();
    (!name.is_empty()).then(|| name.to_string())
}

fn count(conn: &Connection, table: &str) -> i64 {
    conn.query_row(&format!("SELECT COUNT(*) FROM {table}"), [], |row| {
        row.get::<_, i64>(0)
    })
    .unwrap_or(0)
}

fn count_where(conn: &Connection, table: &str, predicate: &str) -> i64 {
    conn.query_row(
        &format!("SELECT COUNT(*) FROM {table} WHERE {predicate}"),
        [],
        |row| row.get::<_, i64>(0),
    )
    .unwrap_or(0)
}

fn pragma_i64(conn: &Connection, name: &str) -> i64 {
    conn.query_row(&format!("PRAGMA {name}"), [], |row| row.get::<_, i64>(0))
        .unwrap_or(0)
}

/// Loads a text file tail (last `max_lines` lines) for the logs tab.
pub fn tail_file(path: &Path, max_lines: usize) -> String {
    let Ok(text) = std::fs::read_to_string(path) else {
        return String::new();
    };
    let lines: Vec<&str> = text.lines().collect();
    let start = lines.len().saturating_sub(max_lines);
    lines[start..].join("\n")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_owner_name_from_soul_markdown() {
        let soul = "---\nprojects_dir: ~/Projects\n---\nPersonality: direct.\nThe user's name is \"Sang\"; use it rarely.\n";
        assert_eq!(parse_owner_name(soul).as_deref(), Some("Sang"));
        assert_eq!(parse_owner_name("Personality: direct.\n"), None);
        assert_eq!(parse_owner_name("The user's name is \"\""), None);
    }

    #[test]
    fn loads_saved_speakers_and_owner_files() {
        let dir = std::env::temp_dir().join(format!("jarvis-dashboard-speakers-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("speakers")).unwrap();
        std::fs::write(
            dir.join("SOUL.md"),
            "---\nprojects_dir: ~/Projects\n---\nThe user's name is \"Sang\"; use it rarely.\n",
        )
        .unwrap();
        std::fs::write(dir.join("owner_voice.npy"), b"fake").unwrap();
        std::fs::write(
            dir.join("speakers").join("speakers.json"),
            r#"{"voice_2": {"name": "Alex", "clips": 3, "speech_secs": 9.4, "vouched": false},
                "voice_1": {"name": "David", "clips": 6, "speech_secs": 18.2, "vouched": true}}"#,
        )
        .unwrap();

        let cfg = Config {
            jarvis_home: dir.clone(),
            home: dir.join("brain"),
            db_path: dir.join("brain/state.db"),
            sessions_dir: dir.join("brain/sessions"),
            traces_dir: dir.join("brain/traces"),
            skills_dir: dir.join("brain/skills"),
            notes_dir: dir.join("brain/memory/progress"),
            logs_dir: dir.join("logs"),
            host: "127.0.0.1".into(),
            port: 8643,
            api_key: String::new(),
        };
        let loaded = load_disk_speakers(&cfg);
        assert_eq!(loaded.owner_name.as_deref(), Some("Sang"));
        assert!(loaded.owner_voiceprint);
        assert_eq!(loaded.guests.len(), 2);
        assert_eq!(loaded.guests[0].name, "Alex");
        assert_eq!(loaded.guests[1].id, "voice_1");
        assert!(loaded.guests[1].vouched);
        let _ = std::fs::remove_dir_all(&dir);
    }
}

//! Incremental tailer for `pi --mode json` session transcripts (`sessions/**/*.jsonl`).

use std::io::{Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::Sender;
use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime};

use serde_json::Value;

use crate::app::AppEvent;
use crate::config::Config;
use crate::model::{Turn, TurnKind};

const MAX_PARTIAL_BYTES: usize = 4 * 1024 * 1024;

struct TailState {
    path: Option<PathBuf>,
    offset: u64,
    partial: Vec<u8>,
}

pub fn tailer_loop(
    cfg: Config,
    pin: Arc<Mutex<Option<PathBuf>>>,
    tx: Sender<AppEvent>,
    stop: Arc<AtomicBool>,
) {
    let mut state = TailState {
        path: None,
        offset: 0,
        partial: Vec::new(),
    };
    while !stop.load(Ordering::Relaxed) {
        let pinned = pin.lock().ok().and_then(|guard| guard.clone());
        let target = pinned.or_else(|| newest_session(&cfg.sessions_dir));
        if let Some(path) = target {
            if state.path.as_ref() != Some(&path) {
                state.path = Some(path.clone());
                state.offset = 0;
                state.partial.clear();
                let _ = tx.send(AppEvent::SessionReset { path: path.clone() });
            }
            if let Ok(meta) = std::fs::metadata(&path) {
                if meta.len() < state.offset {
                    state.offset = 0;
                    state.partial.clear();
                    let _ = tx.send(AppEvent::SessionReset { path: path.clone() });
                }
                if meta.len() > state.offset {
                    match read_new_turns(&path, &mut state) {
                        Ok(turns) if !turns.is_empty() => {
                            let _ = tx.send(AppEvent::SessionAppend {
                                path: path.clone(),
                                turns,
                            });
                        }
                        Ok(_) => {}
                        Err(_) => {}
                    }
                }
            }
        }
        std::thread::sleep(Duration::from_millis(350));
    }
}

fn read_new_turns(path: &Path, state: &mut TailState) -> std::io::Result<Vec<Turn>> {
    let mut file = std::fs::File::open(path)?;
    file.seek(SeekFrom::Start(state.offset))?;
    let mut raw = Vec::new();
    file.read_to_end(&mut raw)?;
    state.offset += raw.len() as u64;
    state.partial.extend_from_slice(&raw);

    let mut turns = Vec::new();
    while let Some(idx) = state.partial.iter().position(|&b| b == b'\n') {
        let line_bytes: Vec<u8> = state.partial.drain(..=idx).collect();
        let decoded = String::from_utf8_lossy(&line_bytes);
        let line = decoded.trim();
        if line.is_empty() {
            continue;
        }
        turns.extend(parse_line(line));
    }
    if state.partial.len() > MAX_PARTIAL_BYTES {
        state.partial.clear();
    }
    Ok(turns)
}

/// Finds the most recently modified `.jsonl` under the sessions directory (depth-capped walk).
pub fn newest_session(root: &Path) -> Option<PathBuf> {
    let mut best: Option<(SystemTime, PathBuf)> = None;
    walk(root, 0, &mut best);
    best.map(|(_, path)| path)
}

fn walk(dir: &Path, depth: usize, best: &mut Option<(SystemTime, PathBuf)>) {
    if depth > 4 {
        return;
    }
    let Ok(entries) = std::fs::read_dir(dir) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        let Ok(meta) = entry.metadata() else {
            continue;
        };
        if meta.is_dir() {
            walk(&path, depth + 1, best);
            continue;
        }
        if path.extension().and_then(|e| e.to_str()) != Some("jsonl") {
            continue;
        }
        let modified = meta.modified().unwrap_or(SystemTime::UNIX_EPOCH);
        let replace = match best {
            Some((current, _)) => modified > *current,
            None => true,
        };
        if replace {
            *best = Some((modified, path));
        }
    }
}

/// Parses one `pi --mode json` line into zero or more renderable turns.
pub fn parse_line(line: &str) -> Vec<Turn> {
    let Ok(value) = serde_json::from_str::<Value>(line) else {
        return vec![Turn::new(TurnKind::Meta, "raw", line)];
    };
    let entry_type = value.get("type").and_then(Value::as_str).unwrap_or("");
    if entry_type == "message_update" {
        // Streaming deltas duplicate the final `message_end` content.
        return Vec::new();
    }
    if entry_type == "session" {
        let id = value.get("id").and_then(Value::as_str).unwrap_or("?");
        return vec![Turn::new(TurnKind::Meta, id, "")];
    }

    let msg = value.get("message").cloned().unwrap_or_else(|| value.clone());
    let role = msg.get("role").and_then(Value::as_str).unwrap_or("");
    let cost = extract_cost(&value);
    let content = msg.get("content").cloned().unwrap_or(Value::Null);

    let mut turns: Vec<Turn> = match role {
        "user" => {
            let text = extract_text(&content);
            if text.trim().is_empty() {
                Vec::new()
            } else {
                vec![Turn::new(TurnKind::User, "USER", text)]
            }
        }
        "assistant" => assistant_turns(&content),
        "toolResult" => {
            let tool = msg
                .get("toolName")
                .and_then(Value::as_str)
                .unwrap_or("tool")
                .to_string();
            let is_error = msg.get("isError").and_then(Value::as_bool).unwrap_or(false);
            let text = extract_text(&content);
            let mut turn = Turn::new(
                TurnKind::ToolResult,
                if is_error {
                    format!("{tool} · error")
                } else {
                    format!("{tool} · result")
                },
                text,
            );
            turn.is_error = is_error;
            vec![turn]
        }
        "system" => {
            let text = extract_text(&content);
            if text.trim().is_empty() {
                Vec::new()
            } else {
                vec![Turn::new(TurnKind::System, "SYSTEM", text)]
            }
        }
        _ => Vec::new(),
    };

    if cost > 0.0 {
        if let Some(first) = turns.first_mut() {
            first.cost = cost;
        }
    }
    turns
}

fn assistant_turns(content: &Value) -> Vec<Turn> {
    match content {
        Value::String(text) => {
            if text.trim().is_empty() {
                Vec::new()
            } else {
                vec![Turn::new(TurnKind::Assistant, "ASSISTANT", text.clone())]
            }
        }
        Value::Array(items) => {
            let mut turns = Vec::new();
            for item in items {
                let item_type = item.get("type").and_then(Value::as_str).unwrap_or("");
                match item_type {
                    "text" => {
                        let text = item.get("text").and_then(Value::as_str).unwrap_or("");
                        if !text.trim().is_empty() {
                            turns.push(Turn::new(TurnKind::Assistant, "ASSISTANT", text));
                        }
                    }
                    "toolCall" => {
                        let name = item
                            .get("name")
                            .and_then(Value::as_str)
                            .unwrap_or("tool")
                            .to_string();
                        let args = item
                            .get("arguments")
                            .or_else(|| item.get("input"))
                            .map(|v| v.to_string())
                            .unwrap_or_default();
                        turns.push(Turn::new(
                            TurnKind::Tool,
                            format!("» {name}"),
                            truncate_chars(&args, 600),
                        ));
                    }
                    _ => {}
                }
            }
            turns
        }
        _ => Vec::new(),
    }
}

fn extract_text(content: &Value) -> String {
    match content {
        Value::String(text) => text.clone(),
        Value::Array(items) => items
            .iter()
            .filter_map(|item| {
                if item.get("type").and_then(Value::as_str) == Some("text") {
                    item.get("text").and_then(Value::as_str).map(str::to_string)
                } else {
                    None
                }
            })
            .collect::<Vec<_>>()
            .join("\n"),
        _ => String::new(),
    }
}

fn extract_cost(value: &Value) -> f64 {
    if let Some(cost) = value.get("cost_usd").and_then(Value::as_f64) {
        return cost;
    }
    if let Some(usage) = value.get("usage") {
        if let Some(cost) = usage.get("cost_usd").and_then(Value::as_f64) {
            return cost;
        }
        if let Some(cost) = usage.get("cost").and_then(Value::as_f64) {
            return cost;
        }
        if let Some(total) = usage
            .get("cost")
            .and_then(|c| c.get("total"))
            .and_then(Value::as_f64)
        {
            return total;
        }
    }
    if let Some(msg) = value.get("message") {
        if msg.get("role").and_then(Value::as_str) == Some("assistant") {
            return 0.0;
        }
    }
    0.0
}

fn truncate_chars(text: &str, limit: usize) -> String {
    if text.chars().count() <= limit {
        return text.to_string();
    }
    let clipped: String = text.chars().take(limit).collect();
    format!("{clipped}…")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_assistant_text_and_tool_calls() {
        let line = r#"{"type":"message_end","cost_usd":0.02,"message":{"role":"assistant","content":[{"type":"text","text":"Looking into it."},{"type":"toolCall","name":"read","arguments":{"path":"src/x.py"}}]}}"#;
        let turns = parse_line(line);
        assert_eq!(turns.len(), 2);
        assert_eq!(turns[0].kind, TurnKind::Assistant);
        assert_eq!(turns[0].body, "Looking into it.");
        assert_eq!(turns[0].cost, 0.02);
        assert_eq!(turns[1].kind, TurnKind::Tool);
        assert!(turns[1].title.contains("read"));
    }

    #[test]
    fn skips_streaming_deltas() {
        let line = r#"{"type":"message_update","message":{"role":"assistant","content":"par"}}"#;
        assert!(parse_line(line).is_empty());
    }

    #[test]
    fn flags_tool_errors() {
        let line = r#"{"message":{"role":"toolResult","toolName":"bash","isError":true,"content":"boom"}}"#;
        let turns = parse_line(line);
        assert_eq!(turns.len(), 1);
        assert!(turns[0].is_error);
        assert!(turns[0].title.contains("error"));
    }

    #[test]
    fn parses_user_string_content() {
        let line = r#"{"message":{"role":"user","content":"fix the bug"}}"#;
        let turns = parse_line(line);
        assert_eq!(turns.len(), 1);
        assert_eq!(turns[0].kind, TurnKind::User);
        assert_eq!(turns[0].body, "fix the bug");
    }

    #[test]
    fn read_new_turns_recovers_from_invalid_utf8_and_caps_partial() {
        use std::io::Write;

        let path = std::env::temp_dir().join(format!(
            "jarvis-dashboard-session-utf8-{}.jsonl",
            std::process::id()
        ));
        let mut raw = Vec::new();
        raw.extend_from_slice(br#"{"message":{"role":"user","content":"first valid"}}"#);
        raw.push(b'\n');
        raw.extend_from_slice(&[b'b', b'a', b'd', 0xff, 0xfe, b'\n']);
        raw.extend_from_slice(br#"{"message":{"role":"user","content":"second valid"}}"#);
        raw.push(b'\n');
        std::fs::write(&path, &raw).unwrap();

        let mut state = TailState {
            path: Some(path.clone()),
            offset: 0,
            partial: Vec::new(),
        };
        let turns = read_new_turns(&path, &mut state).unwrap();
        let user_bodies: Vec<&str> = turns
            .iter()
            .filter(|t| t.kind == TurnKind::User)
            .map(|t| t.body.as_str())
            .collect();
        assert_eq!(user_bodies, vec!["first valid", "second valid"]);
        assert_eq!(state.offset, raw.len() as u64);

        // Appending another turn after invalid bytes keeps tailing cleanly.
        let mut file = std::fs::OpenOptions::new().append(true).open(&path).unwrap();
        writeln!(file, r#"{{"message":{{"role":"user","content":"third valid"}}}}"#).unwrap();
        let more = read_new_turns(&path, &mut state).unwrap();
        assert_eq!(more.len(), 1);
        assert_eq!(more[0].body, "third valid");

        // An unbounded line with no newline clears the partial buffer once it exceeds the cap.
        let giant = vec![b'x'; MAX_PARTIAL_BYTES + 1];
        file.write_all(&giant).unwrap();
        let _ = read_new_turns(&path, &mut state).unwrap();
        assert!(state.partial.is_empty());

        let _ = std::fs::remove_file(&path);
    }
}

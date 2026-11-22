//! Tiny loopback HTTP client (plain HTTP) with an SSE reader that understands both `Content-Length` and
//! `Transfer-Encoding: chunked` responses.
//!
//! Deliberately dependency-free: the daemon is same-host, so TLS and a full HTTP stack would only add risk to a
//! process that must stay up for weeks. The host may be a name (`mac-mini.local`) when `BRAIN_HOST` points at
//! another machine, so addresses are resolved, not parsed.

use std::io::{BufRead, BufReader, Read, Write};
use std::net::{SocketAddr, TcpStream, ToSocketAddrs};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::Sender;
use std::sync::Arc;
use std::time::Duration;

use crate::app::AppEvent;
use crate::config::Config;

fn socket_addr(cfg: &Config) -> Result<SocketAddr, String> {
    let endpoint = cfg.endpoint();
    endpoint
        .to_socket_addrs()
        .map_err(|e| format!("bad endpoint {endpoint}: {e}"))?
        .next()
        .ok_or_else(|| format!("bad endpoint {endpoint}: resolves to nothing"))
}

/// `POST` a JSON body to the daemon. Returns the raw response body on HTTP 200.
pub fn post_json(cfg: &Config, path: &str, body: &str) -> Result<String, String> {
    let addr = socket_addr(cfg)?;
    let mut stream = TcpStream::connect_timeout(&addr, Duration::from_millis(2000))
        .map_err(|e| format!("connect {}: {e}", cfg.endpoint()))?;
    stream
        .set_read_timeout(Some(Duration::from_secs(15)))
        .map_err(|e| e.to_string())?;
    stream
        .set_write_timeout(Some(Duration::from_secs(5)))
        .map_err(|e| e.to_string())?;

    let request = format!(
        "POST {path} HTTP/1.1\r\nHost: {host}:{port}\r\nAuthorization: Bearer {key}\r\n\
         Content-Type: application/json\r\nContent-Length: {len}\r\nConnection: close\r\n\r\n{body}",
        host = cfg.host,
        port = cfg.port,
        key = cfg.api_key,
        len = body.len(),
    );
    stream
        .write_all(request.as_bytes())
        .map_err(|e| format!("write: {e}"))?;

    let mut raw = Vec::new();
    stream
        .read_to_end(&mut raw)
        .map_err(|e| format!("read: {e}"))?;
    let text = String::from_utf8_lossy(&raw);
    let (head, payload) = text.split_once("\r\n\r\n").unwrap_or((&text, ""));
    let status_line = head.lines().next().unwrap_or("<no status>");
    if !status_line.contains(" 200") {
        return Err(format!("{status_line} — {}", payload.trim()));
    }
    Ok(payload.to_string())
}

/// Reconnecting SSE loop. Runs on its own thread for the lifetime of the process.
pub fn sse_loop(cfg: Config, tx: Sender<AppEvent>, stop: Arc<AtomicBool>) {
    while !stop.load(Ordering::Relaxed) {
        match sse_once(&cfg, &tx, &stop) {
            Ok(()) => {}
            Err(err) => {
                let _ = tx.send(AppEvent::Daemon(false, Some(err)));
            }
        }
        if stop.load(Ordering::Relaxed) {
            break;
        }
        std::thread::sleep(Duration::from_millis(1200));
    }
}

fn sse_once(cfg: &Config, tx: &Sender<AppEvent>, stop: &Arc<AtomicBool>) -> Result<(), String> {
    let addr = socket_addr(cfg)?;
    let mut stream = TcpStream::connect_timeout(&addr, Duration::from_millis(2000))
        .map_err(|e| format!("connect {}: {e}", cfg.endpoint()))?;
    // The daemon emits a heartbeat every 5s; a 20s silence means the link is dead.
    stream
        .set_read_timeout(Some(Duration::from_secs(20)))
        .map_err(|e| e.to_string())?;
    stream
        .set_write_timeout(Some(Duration::from_secs(5)))
        .map_err(|e| e.to_string())?;

    let request = format!(
        "GET /v1/telemetry/events HTTP/1.1\r\nHost: {host}:{port}\r\n\
         Authorization: Bearer {key}\r\nAccept: text/event-stream\r\n\
         Cache-Control: no-cache\r\nConnection: keep-alive\r\n\r\n",
        host = cfg.host,
        port = cfg.port,
        key = cfg.api_key,
    );
    stream
        .write_all(request.as_bytes())
        .map_err(|e| format!("write: {e}"))?;

    let mut reader = BufReader::new(stream);
    let mut chunked = false;
    let mut status_ok = false;
    let mut header_bytes = 0usize;
    loop {
        let mut line = String::new();
        let read = reader
            .read_line(&mut line)
            .map_err(|e| format!("headers: {e}"))?;
        if read == 0 {
            return Err("connection closed while reading headers".to_string());
        }
        header_bytes += read;
        if header_bytes > 64 * 1024 {
            return Err("response headers too large".to_string());
        }
        let trimmed = line.trim_end_matches(['\r', '\n']);
        if trimmed.is_empty() {
            break;
        }
        let lower = trimmed.to_ascii_lowercase();
        if lower.starts_with("http/") {
            status_ok = trimmed.contains(" 200");
        }
        if lower.starts_with("transfer-encoding:") && lower.contains("chunked") {
            chunked = true;
        }
    }
    if !status_ok {
        return Err("telemetry stream rejected (check BRAIN_API_KEY)".to_string());
    }
    let _ = tx.send(AppEvent::Daemon(true, None));

    let mut pending = String::new();
    loop {
        if stop.load(Ordering::Relaxed) {
            return Ok(());
        }
        let chunk = if chunked {
            read_chunk(&mut reader)?
        } else {
            read_some(&mut reader)?
        };
        pending.push_str(&String::from_utf8_lossy(&chunk));
        while let Some(idx) = pending.find('\n') {
            let line: String = pending.drain(..=idx).collect();
            let line = line.trim_end_matches(['\r', '\n']);
            let Some(payload) = line.strip_prefix("data:") else {
                continue;
            };
            let payload = payload.trim();
            if payload.is_empty() {
                continue;
            }
            match serde_json::from_str::<serde_json::Value>(payload) {
                Ok(value) => {
                    let _ = tx.send(AppEvent::Telemetry(
                        crate::model::TelemetryEvent::from_value(value),
                    ));
                }
                Err(_) => continue,
            }
        }
        if pending.len() > 4 * 1024 * 1024 {
            pending.clear();
        }
    }
}

fn read_chunk<R: BufRead>(reader: &mut R) -> Result<Vec<u8>, String> {
    let mut size_line = String::new();
    let read = reader
        .read_line(&mut size_line)
        .map_err(|e| format!("chunk header: {e}"))?;
    if read == 0 {
        return Err("stream closed".to_string());
    }
    let token = size_line.trim().split(';').next().unwrap_or("0").to_string();
    let size = usize::from_str_radix(&token, 16)
        .map_err(|e| format!("bad chunk size {token:?}: {e}"))?;
    if size == 0 {
        // Drain the optional trailer section, then report end-of-stream.
        loop {
            let mut trailer = String::new();
            let n = reader
                .read_line(&mut trailer)
                .map_err(|e| format!("chunk trailer: {e}"))?;
            if n == 0 || trailer.trim().is_empty() {
                break;
            }
        }
        return Err("stream closed by daemon".to_string());
    }
    let mut buf = vec![0u8; size];
    reader
        .read_exact(&mut buf)
        .map_err(|e| format!("chunk body: {e}"))?;
    let mut crlf = String::new();
    let _ = reader.read_line(&mut crlf);
    Ok(buf)
}

fn read_some<R: Read>(reader: &mut R) -> Result<Vec<u8>, String> {
    let mut buf = [0u8; 8192];
    match reader.read(&mut buf) {
        Ok(0) => Err("stream closed".to_string()),
        Ok(n) => Ok(buf[..n].to_vec()),
        Err(e) => Err(format!("read: {e}")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpListener;

    fn test_config(port: u16) -> Config {
        Config {
            home: std::path::PathBuf::from("/tmp"),
            db_path: std::path::PathBuf::from("/tmp/state.db"),
            sessions_dir: std::path::PathBuf::from("/tmp/sessions"),
            traces_dir: std::path::PathBuf::from("/tmp/traces"),
            skills_dir: std::path::PathBuf::from("/tmp/skills"),
            notes_dir: std::path::PathBuf::from("/tmp/notes"),
            logs_dir: std::path::PathBuf::from("/tmp/logs"),
            host: "127.0.0.1".to_string(),
            port,
            api_key: "demo-token".to_string(),
        }
    }

    #[test]
    fn post_json_sends_bearer_token_and_reads_body() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let server = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut buf = [0u8; 4096];
            let read = stream.read(&mut buf).unwrap();
            let request = String::from_utf8_lossy(&buf[..read]).to_string();
            assert!(request.starts_with("POST /v1/ask HTTP/1.1"), "{request}");
            assert!(request.contains("Authorization: Bearer demo-token"), "{request}");
            assert!(request.contains(r#""input":"hello""#), "{request}");
            stream
                .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 18\r\n\r\n{\"spoken\":\"hi\"}  ")
                .unwrap();
        });

        let cfg = test_config(addr.port());
        let body = post_json(&cfg, "/v1/ask", r#"{"input":"hello"}"#).unwrap();
        assert!(body.contains("spoken"), "{body}");
        server.join().unwrap();
    }

    #[test]
    fn post_json_surfaces_non_200_status() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let server = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut buf = [0u8; 4096];
            let _ = stream.read(&mut buf);
            stream
                .write_all(b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 17\r\n\r\n{\"error\":\"nope\"}")
                .unwrap();
        });

        let cfg = test_config(addr.port());
        let err = post_json(&cfg, "/v1/ask", "{}").unwrap_err();
        assert!(err.contains("401"), "{err}");
        server.join().unwrap();
    }
}

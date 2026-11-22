//! Runtime configuration: paths, loopback endpoint, and bearer token discovery.

use std::collections::HashMap;
use std::path::PathBuf;

#[derive(Debug, Clone)]
pub struct Config {
    pub home: PathBuf,
    pub db_path: PathBuf,
    pub sessions_dir: PathBuf,
    pub traces_dir: PathBuf,
    pub skills_dir: PathBuf,
    pub notes_dir: PathBuf,
    pub logs_dir: PathBuf,
    pub host: String,
    pub port: u16,
    pub api_key: String,
}

impl Config {
    pub fn load() -> Self {
        // One home for the voice harness, the brain and this cockpit; `jarvis` exports these to us.
        let jarvis_home = std::env::var("JARVIS_HOME")
            .ok()
            .filter(|v| !v.trim().is_empty())
            .map(|v| expand_path(v.trim()))
            .unwrap_or_else(|| expand_path("~/.config/jarvis"));
        let home = brain_home(&jarvis_home);

        // `JARVIS_HOME/.env` wins over the pre-merge `~/.config/superbrain/.env` where both set a key.
        let mut env_file = parse_env_file(&home.join(".env"));
        for (key, value) in parse_env_file(&jarvis_home.join(".env")) {
            env_file.insert(key, value);
        }
        let lookup = |key: &str| -> Option<String> {
            std::env::var(key)
                .ok()
                .filter(|v| !v.trim().is_empty())
                .or_else(|| env_file.get(key).cloned().filter(|v| !v.trim().is_empty()))
        };

        let host = lookup("BRAIN_HOST")
            .or_else(|| lookup("HOST"))
            .unwrap_or_else(|| "127.0.0.1".to_string());
        let port = lookup("BRAIN_PORT")
            .or_else(|| lookup("PORT"))
            .and_then(|v| v.parse::<u16>().ok())
            .unwrap_or(8643);
        let api_key = lookup("SUPERBRAIN_API_KEY")
            .or_else(|| read_token(&jarvis_home.join("brain_token")))
            .or_else(|| read_token(&home.join("brain_token")))
            .unwrap_or_default();

        Self {
            db_path: home.join("state.db"),
            sessions_dir: home.join("sessions"),
            traces_dir: home.join("traces"),
            skills_dir: home.join("skills"),
            notes_dir: home.join("memory").join("progress"),
            logs_dir: home.join("logs"),
            home,
            host,
            port,
            api_key,
        }
    }

    pub fn endpoint(&self) -> String {
        format!("{}:{}", self.host, self.port)
    }

    pub fn has_api_key(&self) -> bool {
        !self.api_key.trim().is_empty()
    }
}

/// The brain's state folder: `SUPERBRAIN_HOME`/`JARVIS_BRAIN_HOME`, else `JARVIS_HOME/brain`, else the
/// pre-merge `~/.config/superbrain` until `jarvis init` moves it.
pub fn brain_home(jarvis_home: &std::path::Path) -> PathBuf {
    let explicit = std::env::var("JARVIS_BRAIN_HOME")
        .ok()
        .or_else(|| std::env::var("SUPERBRAIN_HOME").ok())
        .filter(|v| !v.trim().is_empty());
    if let Some(raw) = explicit {
        return expand_path(raw.trim());
    }
    let fresh = jarvis_home.join("brain");
    let legacy = expand_path("~/.config/superbrain");
    if !fresh.exists() && legacy.exists() {
        legacy
    } else {
        fresh
    }
}

/// The shared bearer token file `jarvis` makes: `JARVIS_HOME/brain_token`, 0600.
fn read_token(path: &std::path::Path) -> Option<String> {
    let text = std::fs::read_to_string(path).ok()?;
    let token = text.trim().to_string();
    (!token.is_empty()).then_some(token)
}

/// Expands a leading `~` using `$HOME`.
pub fn expand_path(raw: &str) -> PathBuf {
    let trimmed = raw.trim();
    if let Some(rest) = trimmed.strip_prefix("~/") {
        if let Some(home) = std::env::var_os("HOME") {
            return PathBuf::from(home).join(rest);
        }
    }
    PathBuf::from(trimmed)
}

/// Minimal `KEY=VALUE` parser for `JARVIS_HOME/.env` (quotes and `export` tolerated).
pub fn parse_env_file(path: &std::path::Path) -> HashMap<String, String> {
    let mut out = HashMap::new();
    let Ok(text) = std::fs::read_to_string(path) else {
        return out;
    };
    for raw in text.lines() {
        let line = raw.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let line = line.strip_prefix("export ").unwrap_or(line);
        let Some((key, value)) = line.split_once('=') else {
            continue;
        };
        let key = key.trim();
        if key.is_empty() {
            continue;
        }
        let mut value = value.trim().to_string();
        if value.len() >= 2
            && ((value.starts_with('"') && value.ends_with('"'))
                || (value.starts_with('\'') && value.ends_with('\'')))
        {
            value = value[1..value.len() - 1].to_string();
        }
        out.insert(key.to_string(), value);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_env_file_with_quotes_and_comments() {
        let dir = std::env::temp_dir().join(format!("jarvis-top-cfg-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join(".env");
        std::fs::write(
            &path,
            "# comment\nSUPERBRAIN_API_KEY=\"abc123\"\nBRAIN_PORT=9999\nexport BRAIN_HOST=0.0.0.0\n",
        )
        .unwrap();
        let parsed = parse_env_file(&path);
        assert_eq!(parsed.get("SUPERBRAIN_API_KEY").unwrap(), "abc123");
        assert_eq!(parsed.get("BRAIN_PORT").unwrap(), "9999");
        assert_eq!(parsed.get("BRAIN_HOST").unwrap(), "0.0.0.0");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn expands_home_prefix() {
        std::env::set_var("HOME", "/tmp/sb-home-test");
        assert_eq!(expand_path("~/x/y"), PathBuf::from("/tmp/sb-home-test/x/y"));
        assert_eq!(expand_path("/abs/path"), PathBuf::from("/abs/path"));
    }
}

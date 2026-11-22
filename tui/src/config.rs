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
        let home = std::env::var("SUPERBRAIN_HOME")
            .ok()
            .filter(|v| !v.trim().is_empty())
            .map(|v| expand_path(v.trim()))
            .unwrap_or_else(|| expand_path("~/.config/superbrain"));

        let env_file = parse_env_file(&home.join(".env"));
        let lookup = |key: &str| -> Option<String> {
            std::env::var(key)
                .ok()
                .filter(|v| !v.trim().is_empty())
                .or_else(|| env_file.get(key).cloned().filter(|v| !v.trim().is_empty()))
        };

        let host = lookup("HOST").unwrap_or_else(|| "127.0.0.1".to_string());
        let port = lookup("PORT")
            .and_then(|v| v.parse::<u16>().ok())
            .unwrap_or(8643);
        let api_key = lookup("SUPERBRAIN_API_KEY").unwrap_or_default();

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

/// Minimal `KEY=VALUE` parser for `~/.config/superbrain/.env` (quotes and `export` tolerated).
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
        let dir = std::env::temp_dir().join(format!("sb-top-cfg-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join(".env");
        std::fs::write(
            &path,
            "# comment\nSUPERBRAIN_API_KEY=\"abc123\"\nPORT=9999\nexport HOST=0.0.0.0\n",
        )
        .unwrap();
        let parsed = parse_env_file(&path);
        assert_eq!(parsed.get("SUPERBRAIN_API_KEY").unwrap(), "abc123");
        assert_eq!(parsed.get("PORT").unwrap(), "9999");
        assert_eq!(parsed.get("HOST").unwrap(), "0.0.0.0");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn expands_home_prefix() {
        std::env::set_var("HOME", "/tmp/sb-home-test");
        assert_eq!(expand_path("~/x/y"), PathBuf::from("/tmp/sb-home-test/x/y"));
        assert_eq!(expand_path("/abs/path"), PathBuf::from("/abs/path"));
    }
}

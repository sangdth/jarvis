//! Runtime configuration: paths, loopback endpoint, and bearer token discovery.

use std::collections::HashMap;
use std::path::{Path, PathBuf};

#[derive(Debug, Clone)]
pub struct Config {
    pub jarvis_home: PathBuf,
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
        // One home for the voice harness, the brain and this dashboard; `jarvis` exports these to us.
        let jarvis_home = std::env::var("JARVIS_HOME")
            .ok()
            .filter(|v| !v.trim().is_empty())
            .map(|v| expand_path(v.trim()))
            .or_else(|| {
                std::env::current_exe()
                    .ok()
                    .map(|p| std::fs::canonicalize(&p).unwrap_or(p))
                    .and_then(|p| workspace_root_from(&p))
            })
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
            .unwrap_or_else(|| "0.0.0.0".to_string());
        let port = lookup("BRAIN_PORT")
            .or_else(|| lookup("PORT"))
            .and_then(|v| v.parse::<u16>().ok())
            .unwrap_or(8643);
        let api_key = resolve_api_key(lookup, &jarvis_home, &home);

        Self {
            db_path: home.join("state.db"),
            sessions_dir: home.join("sessions"),
            traces_dir: home.join("traces"),
            skills_dir: home.join("skills"),
            notes_dir: home.join("memory").join("progress"),
            logs_dir: jarvis_home.join("logs"),
            jarvis_home,
            home,
            host,
            port,
            api_key,
        }
    }

    pub fn speakers_path(&self) -> PathBuf {
        self.jarvis_home.join("speakers").join("speakers.json")
    }

    pub fn soul_path(&self) -> PathBuf {
        self.jarvis_home.join("SOUL.md")
    }

    pub fn owner_voiceprint_path(&self) -> PathBuf {
        self.jarvis_home.join("owner_voice.npy")
    }

    pub fn endpoint(&self) -> String {
        let host = self.connect_host();
        if host.contains(':') && !host.starts_with('[') {
            format!("[{host}]:{}", self.port)
        } else {
            format!("{host}:{}", self.port)
        }
    }

    /// The address to dial: a wildcard bind address (`0.0.0.0`, `::`) means "every interface", so a client
    /// on this Mac must use loopback instead.
    pub fn connect_host(&self) -> &str {
        match self.host.trim() {
            "" | "0.0.0.0" | "::" | "[::]" => "127.0.0.1",
            other => other,
        }
    }

    pub fn has_api_key(&self) -> bool {
        !self.api_key.trim().is_empty()
    }
}

/// Nearest ancestor directory containing a uv workspace `pyproject.toml`.
pub fn workspace_root_from(start: &Path) -> Option<PathBuf> {
    for dir in start.ancestors() {
        let pyproject = dir.join("pyproject.toml");
        if !pyproject.is_file() {
            continue;
        }
        if let Ok(text) = std::fs::read_to_string(&pyproject) {
            if text.contains("[tool.uv.workspace]") {
                return Some(dir.to_path_buf());
            }
        }
    }
    None
}

fn resolve_api_key(
    lookup: impl Fn(&str) -> Option<String>,
    jarvis_home: &Path,
    home: &Path,
) -> String {
    lookup("JARVIS_TOKEN")
        .or_else(|| lookup("BRAIN_API_KEY"))
        .or_else(|| lookup("SUPERBRAIN_API_KEY")) // the name before the rename
        .or_else(|| read_token(&jarvis_home.join("brain_token")))
        .or_else(|| read_token(&home.join("brain_token")))
        .unwrap_or_default()
}

/// The brain's state folder: `BRAIN_HOME` (or the pre-rename `SUPERBRAIN_HOME`), else `JARVIS_HOME/brain`,
/// else — for an installed Jarvis only — the pre-merge `~/.config/superbrain` until `jarvis init` moves it.
pub fn brain_home(jarvis_home: &Path) -> PathBuf {
    let explicit = std::env::var("BRAIN_HOME")
        .ok()
        .or_else(|| std::env::var("SUPERBRAIN_HOME").ok())
        .filter(|v| !v.trim().is_empty());
    if let Some(raw) = explicit {
        return expand_path(raw.trim());
    }
    let fresh = jarvis_home.join("brain");
    let legacy = expand_path("~/.config/superbrain");
    if !fresh.exists() && jarvis_home == expand_path("~/.config/jarvis") && legacy.exists() {
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
        let dir = std::env::temp_dir().join(format!("jarvis-dashboard-cfg-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join(".env");
        std::fs::write(
            &path,
            "# comment\nBRAIN_API_KEY=\"abc123\"\nBRAIN_PORT=9999\nexport BRAIN_HOST=0.0.0.0\n",
        )
        .unwrap();
        let parsed = parse_env_file(&path);
        assert_eq!(parsed.get("BRAIN_API_KEY").unwrap(), "abc123");
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

    #[test]
    fn workspace_root_from_finds_uv_workspace_ancestor() {
        let base = std::env::temp_dir().join(format!(
            "jarvis-dashboard-ws-{}",
            std::process::id()
        ));
        let repo = base.join("repo");
        let sub_pkg = repo.join("apps").join("dashboard");
        let exe = repo.join("target").join("release").join("jarvis-dashboard");
        let outside = base.join("other").join("bin").join("app");
        std::fs::create_dir_all(&sub_pkg).unwrap();
        std::fs::create_dir_all(exe.parent().unwrap()).unwrap();
        std::fs::create_dir_all(outside.parent().unwrap()).unwrap();
        std::fs::write(repo.join("pyproject.toml"), "[project]\nname = \"jarvis\"\n[tool.uv.workspace]\n").unwrap();
        // Non-workspace pyproject.toml inside a sub-package must be skipped.
        std::fs::write(sub_pkg.join("pyproject.toml"), "[project]\nname = \"sub\"\n").unwrap();

        assert_eq!(workspace_root_from(&exe), Some(repo.clone()));
        assert_eq!(workspace_root_from(&sub_pkg.join("src")), Some(repo));
        assert_eq!(workspace_root_from(&outside), None);

        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn jarvis_token_takes_precedence_over_brain_api_key() {
        let base = std::env::temp_dir().join(format!(
            "jarvis-dashboard-tok-{}",
            std::process::id()
        ));
        let home = base.join("brain");
        std::fs::create_dir_all(&home).unwrap();
        std::fs::write(base.join("brain_token"), "from-file\n").unwrap();

        let mut env_map = HashMap::new();
        env_map.insert("BRAIN_API_KEY".to_string(), "from-brain-key".to_string());
        env_map.insert("JARVIS_TOKEN".to_string(), "from-jarvis-token".to_string());
        let lookup = |k: &str| env_map.get(k).cloned();
        assert_eq!(resolve_api_key(lookup, &base, &home), "from-jarvis-token");

        env_map.remove("JARVIS_TOKEN");
        let lookup = |k: &str| env_map.get(k).cloned();
        assert_eq!(resolve_api_key(lookup, &base, &home), "from-brain-key");

        env_map.clear();
        let lookup = |k: &str| env_map.get(k).cloned();
        assert_eq!(resolve_api_key(lookup, &base, &home), "from-file");

        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn endpoint_brackets_ipv6_literals() {
        let mut cfg = Config {
            jarvis_home: PathBuf::from("/tmp"),
            home: PathBuf::from("/tmp"),
            db_path: PathBuf::from("/tmp/state.db"),
            sessions_dir: PathBuf::from("/tmp/s"),
            traces_dir: PathBuf::from("/tmp/t"),
            skills_dir: PathBuf::from("/tmp/k"),
            notes_dir: PathBuf::from("/tmp/n"),
            logs_dir: PathBuf::from("/tmp/l"),
            host: "::1".to_string(),
            port: 8643,
            api_key: String::new(),
        };
        assert_eq!(cfg.endpoint(), "[::1]:8643");
        cfg.host = "0.0.0.0".to_string();
        assert_eq!(cfg.endpoint(), "127.0.0.1:8643");
    }
}

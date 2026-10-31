//! Флаги и пути: как у server.py, чтобы menubar, launchd и скрипты запускали любой из двух серверов.

use std::ffi::OsString;
use std::path::{Path, PathBuf};

use clap::Parser;

#[derive(Parser)]
#[command(about = "Agent Office: пиксельный офис агентов Claude Code и Codex")]
struct Cli {
    #[arg(long, default_value_t = 8767)]
    port: u16,
    #[arg(long, default_value = "127.0.0.1")]
    host: String,
    /// где хранить офисы папок
    #[arg(long)]
    data: Option<String>,
    /// куда класть скриншоты офиса
    #[arg(long)]
    shots: Option<String>,
    /// выйти, когда умрёт родительский процесс
    #[arg(long)]
    watch_parent: bool,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Config {
    pub port: u16,
    pub host: String,
    pub claude_dir: PathBuf,
    pub codex_dir: PathBuf,
    pub data: PathBuf,
    pub shots: PathBuf,
    pub watch_parent: bool,
}

impl Config {
    /// `env` — переменные окружения: в тестах подставляется своя таблица.
    pub fn parse<I, T>(args: I, env: impl Fn(&str) -> Option<String>) -> Result<Config, clap::Error>
    where
        I: IntoIterator<Item = T>,
        T: Into<OsString> + Clone,
    {
        let cli = Cli::try_parse_from(args)?;
        let home = PathBuf::from(env("HOME").unwrap_or_default());
        let dir = |var: &str, fallback: &str| {
            env(var).filter(|v| !v.is_empty()).map_or_else(|| home.join(fallback), PathBuf::from)
        };
        let claude_dir = dir("CLAUDE_CONFIG_DIR", ".claude");
        let codex_dir = dir("CODEX_HOME", ".codex");
        let data = cli.data.map_or_else(|| claude_dir.join("agent-office"), |p| expand(&p, &home));
        let shots = cli.shots.map_or_else(|| home.join("Pictures/Agent Office"), |p| expand(&p, &home));
        Ok(Config {
            port: cli.port,
            host: cli.host,
            claude_dir,
            codex_dir,
            data,
            shots,
            watch_parent: cli.watch_parent,
        })
    }
}

/// `~` и `~/…` как os.path.expanduser.
fn expand(path: &str, home: &Path) -> PathBuf {
    match path.strip_prefix('~') {
        Some("") => home.to_path_buf(),
        Some(rest) if rest.starts_with('/') => home.join(&rest[1..]),
        _ => PathBuf::from(path),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(args: &[&str], env: &[(&str, &str)]) -> Config {
        let env: Vec<(String, String)> = env.iter().map(|(k, v)| (k.to_string(), v.to_string())).collect();
        let args = std::iter::once("agent-office").chain(args.iter().copied());
        Config::parse(args, |k| env.iter().find(|(n, _)| n == k).map(|(_, v)| v.clone())).unwrap()
    }

    #[test]
    fn defaults_match_server_py() {
        let c = parse(&[], &[("HOME", "/home/demo")]);
        assert_eq!(c.port, 8767);
        assert_eq!(c.host, "127.0.0.1");
        assert_eq!(c.claude_dir, PathBuf::from("/home/demo/.claude"));
        assert_eq!(c.codex_dir, PathBuf::from("/home/demo/.codex"));
        assert_eq!(c.data, PathBuf::from("/home/demo/.claude/agent-office"));
        assert_eq!(c.shots, PathBuf::from("/home/demo/Pictures/Agent Office"));
        assert!(!c.watch_parent);
    }

    #[test]
    fn config_dirs_come_from_the_environment() {
        let c = parse(&[], &[("HOME", "/h"), ("CLAUDE_CONFIG_DIR", "/x/claude"), ("CODEX_HOME", "/x/codex")]);
        assert_eq!(c.claude_dir, PathBuf::from("/x/claude"));
        assert_eq!(c.codex_dir, PathBuf::from("/x/codex"));
        assert_eq!(c.data, PathBuf::from("/x/claude/agent-office"), "--data по умолчанию живёт рядом с журналами");
    }

    #[test]
    fn empty_env_var_falls_back_like_python_or() {
        let c = parse(&[], &[("HOME", "/h"), ("CLAUDE_CONFIG_DIR", "")]);
        assert_eq!(c.claude_dir, PathBuf::from("/h/.claude"));
    }

    #[test]
    fn flags_expand_the_tilde() {
        let args = ["--port", "9000", "--host", "0.0.0.0", "--data", "~/d", "--shots", "~", "--watch-parent"];
        let c = parse(&args, &[("HOME", "/h")]);
        assert_eq!((c.port, c.host.as_str()), (9000, "0.0.0.0"));
        assert_eq!(c.data, PathBuf::from("/h/d"));
        assert_eq!(c.shots, PathBuf::from("/h"));
        assert!(c.watch_parent);
        assert_eq!(parse(&["--data", "/abs/~x"], &[("HOME", "/h")]).data, PathBuf::from("/abs/~x"));
    }

    #[test]
    fn unknown_flag_is_an_error() {
        assert!(Config::parse(["agent-office", "--nope"], |_| None).is_err());
    }
}

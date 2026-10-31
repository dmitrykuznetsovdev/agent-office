//! Процесс сервера: адрес в stdout, выход по сигналу, --watch-parent.

use std::io::{BufRead, BufReader};
use std::net::TcpStream;
use std::os::unix::process::ExitStatusExt;
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

const BIN: &str = env!("CARGO_BIN_EXE_agent-office");

fn start(args: &[&str]) -> (Child, String) {
    let dir = std::env::temp_dir().join(format!("ao-process-{}", std::process::id()));
    let mut child = Command::new(BIN)
        .args(["--port", "0", "--data"])
        .arg(dir.join("data"))
        .arg("--shots")
        .arg(dir.join("shots"))
        .args(args)
        .env("CLAUDE_CONFIG_DIR", dir.join("claude"))
        .env("CODEX_HOME", dir.join("codex"))
        .stdout(Stdio::piped())
        .spawn()
        .expect("сервер запускается");
    let mut line = String::new();
    BufReader::new(child.stdout.take().unwrap()).read_line(&mut line).unwrap();
    (child, line)
}

fn wait(child: &mut Child, limit: Duration) -> std::process::ExitStatus {
    let start = Instant::now();
    loop {
        if let Some(status) = child.try_wait().unwrap() {
            return status;
        }
        assert!(start.elapsed() < limit, "сервер не завершился за {limit:?}");
        std::thread::sleep(Duration::from_millis(50));
    }
}

#[test]
fn prints_its_address_and_listens_there() {
    let (mut child, line) = start(&[]);
    let addr = line.trim().strip_prefix("Agent Office: http://").expect(&line);
    assert!(addr.starts_with("127.0.0.1:"), "{line}");
    TcpStream::connect(addr).expect("порт из строки слушается");
    child.kill().unwrap();
    child.wait().unwrap();
}

#[test]
fn sigterm_ends_it_by_the_same_signal() {
    for sig in [libc::SIGTERM, libc::SIGINT] {
        let (mut child, _) = start(&[]);
        unsafe { libc::kill(child.id() as i32, sig) };
        let status = wait(&mut child, Duration::from_secs(5));
        assert_eq!(status.signal(), Some(sig), "по коду выхода menubar решает, поднимать ли сервер");
    }
}

#[test]
fn watch_parent_exits_when_the_parent_dies() {
    let mut parent = Command::new("sh")
        .args(["-c", "\"$0\" --port 0 --watch-parent >/dev/null & echo $!; wait", BIN])
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    let mut pid = String::new();
    BufReader::new(parent.stdout.take().unwrap()).read_line(&mut pid).unwrap();
    let pid: i32 = pid.trim().parse().unwrap();
    std::thread::sleep(Duration::from_millis(300));
    parent.kill().unwrap();
    parent.wait().unwrap();
    let start = Instant::now();
    while unsafe { libc::kill(pid, 0) } == 0 {
        assert!(start.elapsed() < Duration::from_secs(8), "сервер пережил родителя");
        std::thread::sleep(Duration::from_millis(100));
    }
}

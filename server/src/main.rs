//! Agent Office: пиксельный офис агентов Claude Code и Codex. Корень сборки: флаги, сигналы, сервер.

mod config;
mod http;
mod state;

use std::sync::Arc;
use std::time::Duration;

use tokio::signal::unix::{SignalKind, signal};

use config::Config;
use state::AppState;

#[tokio::main]
async fn main() -> std::io::Result<()> {
    let cfg = Config::parse(std::env::args_os(), |k| std::env::var(k).ok()).unwrap_or_else(|e| e.exit());
    if cfg.watch_parent {
        watch_parent();
    }
    let listener = tokio::net::TcpListener::bind((cfg.host.as_str(), cfg.port)).await?;
    // настоящий порт: при --port 0 его выбирает система, тестам и menubar он нужен из этой строки
    println!("Agent Office: http://{}:{}", cfg.host, listener.local_addr()?.port());
    let app = http::router(Arc::new(AppState { cfg }));
    tokio::select! {
        served = axum::serve(listener, app) => served,
        sig = terminated() => die_by(sig),
    }
}

/// SIGTERM или SIGINT: какой пришёл.
async fn terminated() -> i32 {
    let mut term = signal(SignalKind::terminate()).expect("SIGTERM");
    let mut int = signal(SignalKind::interrupt()).expect("SIGINT");
    tokio::select! {
        _ = term.recv() => libc::SIGTERM,
        _ = int.recv() => libc::SIGINT,
    }
}

/// Выход тем же сигналом, как on_term в server.py: приложение строки меню по коду выхода решает, поднимать ли сервер.
fn die_by(sig: i32) -> ! {
    // генерации офиса (пакет S7) останавливаются здесь, до выхода
    unsafe {
        libc::signal(sig, libc::SIG_DFL);
        libc::raise(sig);
    }
    unreachable!("сигнал {sig} не завершил процесс")
}

/// --watch-parent: родитель умер — процесс переподчинён, getppid сменился.
fn watch_parent() {
    let ppid = unsafe { libc::getppid() };
    std::thread::spawn(move || {
        while unsafe { libc::getppid() } == ppid {
            std::thread::sleep(Duration::from_secs(2));
        }
        std::process::exit(0);
    });
}

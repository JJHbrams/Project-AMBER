use portable_pty::{CommandBuilder, PtySize, native_pty_system};
use std::collections::HashMap;
use std::io::{Read, Write};
use std::sync::{Arc, Mutex};
use tauri::{AppHandle, Emitter};
use serde::{Deserialize, Serialize};
use crate::resolve_cli_workdir;

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct SpawnProviderResponse {
    #[serde(rename = "sessionId")]
    pub session_id: String,
}

/// Windows ConPTY HANDLE은 스레드간 이동이 실제로 안전하지만
/// portable-pty가 Send를 구현하지 않으므로 래퍼로 강제 표시한다.
struct SendMaster(Box<dyn portable_pty::MasterPty>);
unsafe impl Send for SendMaster {}
unsafe impl Sync for SendMaster {}

pub struct ProviderProcess {
    master: SendMaster,
    pub writer: Box<dyn Write + Send>,
    pub killer: Box<dyn portable_pty::ChildKiller + Send + Sync>,
}

pub type ProviderStore = Arc<Mutex<HashMap<String, ProviderProcess>>>;

/// .cmd/.bat 파일은 cmd.exe /C 래퍼로 실행해야 PTY에서 stdin이 동작한다.
fn build_command(cmd: &str, args: &[String]) -> CommandBuilder {
    let lower = cmd.to_ascii_lowercase();
    if lower.ends_with(".cmd") || lower.ends_with(".bat") {
        let mut builder = CommandBuilder::new("cmd.exe");
        builder.arg("/C");
        builder.arg(cmd);
        for a in args {
            builder.arg(a);
        }
        builder
    } else {
        let mut builder = CommandBuilder::new(cmd);
        for a in args {
            builder.arg(a);
        }
        builder
    }
}

fn resolve_command(provider: &str, model: Option<&str>) -> (String, Vec<String>) {
    let home = std::env::var("USERPROFILE")
        .or_else(|_| std::env::var("HOME"))
        .unwrap_or_default();
    match provider {
        "copilot" => (format!("{}/.engram/engram-copilot.cmd", home), vec![]),
        "antigravity"  => (format!("{}/.engram/engram-antigravity.cmd", home), vec![]),
        "claude-code" => {
            let cmd_path = format!("{}/.engram/engram-claude.cmd", home);
            if std::path::Path::new(&cmd_path).exists() {
                (cmd_path, vec![])
            } else {
                ("claude".to_string(), vec![])
            }
        }
        "claude-code-ollama" => {
            let selected_model = model.unwrap_or("qwen3.5:4b").to_string();
            let args = vec!["--model".to_string(), selected_model];
            let cmd_path = format!("{}/.engram/engram-claude.cmd", home);
            if std::path::Path::new(&cmd_path).exists() {
                (cmd_path, args)
            } else {
                ("claude".to_string(), args)
            }
        }
        "ollama" => (
            "ollama".to_string(),
            vec!["run".to_string(), model.unwrap_or("llama3").to_string()],
        ),
        _ => (provider.to_string(), vec![]),
    }
}

#[tauri::command]
pub fn spawn_provider(
    app: AppHandle,
    store: tauri::State<ProviderStore>,
    provider: String,
    model: Option<String>,
) -> Result<SpawnProviderResponse, String> {
    let session_id = format!("{}-{}", provider, uuid_simple());
    let (cmd, args) = resolve_command(&provider, model.as_deref());
    let workdir = resolve_cli_workdir();

    let pty_system = native_pty_system();
    let pair = pty_system
        .openpty(PtySize { rows: 24, cols: 220, pixel_width: 0, pixel_height: 0 })
        .map_err(|e| e.to_string())?;

    let mut cmd_builder = build_command(&cmd, &args);
    cmd_builder.cwd(std::path::Path::new(&workdir));
    let child = pair.slave
        .spawn_command(cmd_builder)
        .map_err(|e| format!("Failed to spawn {cmd} (cwd: {workdir}): {e}"))?;

    // slave 쪽은 spawn 후 즉시 drop — master만 유지
    drop(pair.slave);

    let killer = child.clone_killer();
    let writer = pair.master.take_writer().map_err(|e| e.to_string())?;
    let mut reader = pair.master.try_clone_reader().map_err(|e| e.to_string())?;

    let sid = session_id.clone();
    let app_clone = app.clone();
    std::thread::spawn(move || {
        let mut buf = [0u8; 4096];
        loop {
            match reader.read(&mut buf) {
                Ok(0) | Err(_) => break,
                Ok(n) => {
                    let data = String::from_utf8_lossy(&buf[..n]).to_string();
                    let _ = app_clone.emit("provider://stdout",
                        serde_json::json!({ "sessionId": sid, "data": data }));
                }
            }
        }
        let _ = app_clone.emit("provider://exit",
            serde_json::json!({ "sessionId": sid, "code": null }));
        let _ = app_clone.emit("provider://status",
            serde_json::json!({ "sessionId": sid, "status": "stopped" }));
    });

    let _ = app.emit("provider://status",
        serde_json::json!({ "sessionId": session_id, "status": "running" }));

    store.lock().unwrap().insert(session_id.clone(), ProviderProcess {
        master: SendMaster(pair.master),
        writer,
        killer,
    });

    Ok(SpawnProviderResponse { session_id })
}

#[tauri::command]
pub fn write_stdin(
    store: tauri::State<ProviderStore>,
    session_id: String,
    data: String,
) -> Result<(), String> {
    let mut map = store.lock().unwrap();
    if let Some(proc) = map.get_mut(&session_id) {
        proc.writer.write_all(data.as_bytes()).map_err(|e| e.to_string())?;
        proc.writer.flush().map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[tauri::command]
pub fn resize_pty(
    store: tauri::State<ProviderStore>,
    session_id: String,
    cols: u16,
    rows: u16,
) -> Result<(), String> {
    let map = store.lock().unwrap();
    if let Some(proc) = map.get(&session_id) {
        proc.master.0
            .resize(PtySize { rows, cols, pixel_width: 0, pixel_height: 0 })
            .map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[tauri::command]
pub fn kill_provider(
    app: AppHandle,
    store: tauri::State<ProviderStore>,
    session_id: String,
) -> Result<(), String> {
    let mut map = store.lock().unwrap();
    if let Some(mut proc) = map.remove(&session_id) {
        let _ = proc.killer.kill();
        let _ = app.emit("provider://status",
            serde_json::json!({ "sessionId": session_id, "status": "stopped" }));
    }
    Ok(())
}

fn uuid_simple() -> String {
    use std::time::{SystemTime, UNIX_EPOCH};
    let t = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default();
    format!("{:x}", t.subsec_nanos())
}

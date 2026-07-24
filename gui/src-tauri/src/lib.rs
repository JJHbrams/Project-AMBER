mod provider;
use provider::{spawn_provider, write_stdin, kill_provider, resize_pty, ProviderStore};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use serde_json::{json, Value};

const MCP_BASE: &str = "http://127.0.0.1:17385";

pub(crate) fn resolve_cli_workdir() -> String {
    let home = std::env::var("USERPROFILE")
        .or_else(|_| std::env::var("HOME"))
        .unwrap_or_else(|_| "~".into());

    let from_env = |key: &str| -> Option<String> {
        let raw = std::env::var(key).ok()?;
        let trimmed = raw.trim();
        if trimmed.is_empty() {
            return None;
        }
        let p = PathBuf::from(trimmed);
        if p.exists() {
            Some(p.to_string_lossy().into_owned())
        } else {
            None
        }
    };

    let normalize_dev_cwd = |cwd: PathBuf| -> PathBuf {
        let is_repo_root = |p: &Path| p.join(".git").exists() || p.join("mcp_server.py").exists();

        if cwd.file_name().and_then(|s| s.to_str()) == Some("src-tauri") {
            if let Some(gui_dir) = cwd.parent() {
                if gui_dir.file_name().and_then(|s| s.to_str()) == Some("gui") {
                    if let Some(repo_root) = gui_dir.parent() {
                        if is_repo_root(repo_root) {
                            return repo_root.to_path_buf();
                        }
                    }
                }
            }
        }

        if cwd.file_name().and_then(|s| s.to_str()) == Some("gui") {
            if let Some(repo_root) = cwd.parent() {
                if is_repo_root(repo_root) {
                    return repo_root.to_path_buf();
                }
            }
        }
        cwd
    };

    from_env("ENGRAM_WORKDIR")
        .or_else(|| from_env("PWD"))
        .or_else(|| {
            std::env::current_dir()
                .ok()
                .map(normalize_dev_cwd)
                .map(|p| p.to_string_lossy().into_owned())
        })
        .unwrap_or(home)
}

#[tauri::command]
fn get_system_info() -> Value {
    let username = std::env::var("USERNAME")
        .or_else(|_| std::env::var("USER"))
        .unwrap_or_else(|_| "user".into());
    let home = std::env::var("USERPROFILE")
        .or_else(|_| std::env::var("HOME"))
        .unwrap_or_else(|_| "~".into());
    let cwd = resolve_cli_workdir();
    json!({ "username": username, "home": home, "cwd": cwd })
}

/// MCP 서버 헬스 체크. 연결되면 true.
#[tauri::command]
async fn ping_mcp() -> Result<bool, String> {
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(3))
        .build()
        .map_err(|e| e.to_string())?;
    match client.get(format!("{MCP_BASE}/health")).send().await {
        Ok(r) => Ok(r.status().is_success()),
        Err(_) => Ok(false),
    }
}

/// KG SemanticGraph 전체 그래프 데이터 (노드 + 엣지)
#[tauri::command]
async fn fetch_kg_graph() -> Result<Value, String> {
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(10))
        .build()
        .map_err(|e| e.to_string())?;
    let resp = client
        .get(format!("{MCP_BASE}/api/sg/graph"))
        .send()
        .await
        .map_err(|e| e.to_string())?;
    resp.json::<Value>().await.map_err(|e| e.to_string())
}

/// KG SemanticGraph 통계
#[tauri::command]
async fn get_kg_stats() -> Result<Value, String> {
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(5))
        .build()
        .map_err(|e| e.to_string())?;
    let resp = client
        .get(format!("{MCP_BASE}/api/sg/stats"))
        .send()
        .await
        .map_err(|e| e.to_string())?;
    resp.json::<Value>().await.map_err(|e| e.to_string())
}

/// 범용 MCP API 호출 (tool: "kg_search" | "kg_neighbors")
#[tauri::command]
async fn mcp_query(tool: String, params: Value) -> Result<Value, String> {
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(10))
        .build()
        .map_err(|e| e.to_string())?;
    let url = match tool.as_str() {
        "kg_search"    => format!("{MCP_BASE}/api/sg/search"),
        "kg_neighbors" => format!("{MCP_BASE}/api/sg/neighbors"),
        other          => return Err(format!("Unknown tool: {other}")),
    };
    let resp = client
        .post(&url)
        .json(&params)
        .send()
        .await
        .map_err(|e| e.to_string())?;
    resp.json::<Value>().await.map_err(|e| e.to_string())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let store: ProviderStore = Arc::new(Mutex::new(HashMap::new()));
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .manage(store)
        .invoke_handler(tauri::generate_handler![
            spawn_provider,
            write_stdin,
            kill_provider,
            resize_pty,
            get_system_info,
            ping_mcp,
            fetch_kg_graph,
            get_kg_stats,
            mcp_query,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

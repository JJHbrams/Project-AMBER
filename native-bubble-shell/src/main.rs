#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
use std::{io::{self, BufRead, Read, Write}, sync::{Arc, Mutex}, collections::HashMap};
use tauri::{Emitter, Manager, PhysicalPosition, PhysicalSize, Position, Size};
use serde_json::{json, Value};
#[cfg(windows)]
use windows::Win32::{Foundation::POINT, Graphics::Gdi::{CombineRgn, CreateEllipticRgn, CreatePolygonRgn, CreateRoundRectRgn, DeleteObject, SetWindowRgn, RGN_OR, WINDING}};

const MAX_LINE: usize = 32 * 1024 * 1024;
// Included in rustc's dependency fingerprint so frontendDist changes relink
// the resource library into this executable (the value is never exposed).
const _FRONTEND_REV: &str = env!("NATIVE_BUBBLE_FRONTEND_REV");
const ACTIONS: &[&str] = &["submit","edit","save_edit","cancel_edit","delete","interrupt","send_now","resume_queue","history","close","input_activity","resize_input","presentation_size","hover","dismiss","approval","nudge_reply","nudge_defer","composer_state","speech_history_state"];
type Output = Arc<Mutex<Box<dyn Write + Send>>>;
type Targets = Arc<Mutex<HashMap<String, [f64; 2]>>>;
#[derive(Clone, Copy, PartialEq)]
struct NativeRect { x: i32, y: i32, width: u32, height: u32 }

fn emit(out: &Output, value: Value) { if let Ok(mut w) = out.lock() { let _ = writeln!(w, "{}", value); let _ = w.flush(); } }
fn allowed_action(a: &str) -> bool { ACTIONS.contains(&a) }

#[tauri::command]
fn shell_ready(window: tauri::WebviewWindow, out: tauri::State<Output>) { emit(&out, json!({"type":"ready","window":window.label()})); }
#[tauri::command]
fn shell_action(action: String, action_id: String, request_id: Option<String>, payload: Value, out: tauri::State<Output>) -> Result<(), String> {
  if !allowed_action(&action) || action_id.is_empty() { return Err("invalid_action".into()); }
  emit(&out, json!({"type":"action","action":action,"action_id":action_id,"request_id":request_id,"payload":payload})); Ok(())
}
#[tauri::command]
fn shell_geometry(rect: Value, out: tauri::State<Output>) { emit(&out, json!({"type":"geometry","rect":rect})); }
#[tauri::command]
fn native_rect(window: &tauri::WebviewWindow) -> Option<NativeRect> {
  let (Ok(size), Ok(position)) = (window.inner_size(), window.outer_position()) else { return None };
  Some(NativeRect { x: position.x, y: position.y, width: size.width, height: size.height })
}
fn report_geometry(window: &tauri::WebviewWindow, origin: &str) {
  let (Ok(size), Ok(position)) = (window.inner_size(), window.outer_position()) else { return };
  let scale = window.scale_factor().unwrap_or(1.0);
  let w = size.width as f64; let h = size.height as f64;
  // Minimize and DPI transitions can transiently bypass configured min sizes.
  if !scale.is_finite() || scale <= 0.0 || w < 100.0*scale || h < 80.0*scale { return; }
  let target = if window.label() == "input" {
    window.current_monitor().ok().flatten().map(|m| [m.position().x as f64 + m.size().width as f64/2.0, m.position().y as f64 + m.size().height as f64])
  } else { window.state::<Targets>().lock().ok().and_then(|t| t.get(window.label()).copied()) };
  let target = target.unwrap_or([position.x as f64 + w/2.0, position.y as f64 + h + 100.0]);
  let left=12.0*scale; let right=w-12.0*scale; let top=12.0*scale; let bottom=h-32.0*scale;
  let cx=(left+right)/2.0; let cy=(top+bottom)/2.0;
  let dx=target[0]-position.x as f64-cx; let dy=target[1]-position.y as f64-cy;
  let points;
  if window.label()=="input" || dy >= dx.abs() * (bottom-top)/(right-left).max(1.0) {
    let x=(cx+dx*0.15).clamp(left+38.0*scale,right-38.0*scale);
    points=[[x-13.0*scale,bottom-2.0*scale],[(x+dx/(dy.abs().max(1.0))*22.0*scale).clamp(left,right),bottom+22.0*scale],[x+13.0*scale,bottom-2.0*scale]];
  } else if -dy >= dx.abs()*(bottom-top)/(right-left).max(1.0) {
    points=[[cx-12.0*scale,top+2.0*scale],[cx,2.0*scale],[cx+12.0*scale,top+2.0*scale]];
  } else if dx < 0.0 {
    points=[[left+2.0*scale,cy-12.0*scale],[2.0*scale,cy],[left+2.0*scale,cy+12.0*scale]];
  } else {
    points=[[right-2.0*scale,cy-12.0*scale],[w-2.0*scale,cy],[right-2.0*scale,cy+12.0*scale]];
  }
  let rect=json!({"window":window.label(),"x":position.x,"y":position.y,"width":size.width,"height":size.height,"scale":scale,"origin":origin,"tail_points":points});
  #[cfg(windows)] unsafe {
    let body=CreateRoundRectRgn((left-4.0*scale) as i32,(top-4.0*scale) as i32,(right+4.0*scale) as i32,(bottom+5.0*scale) as i32,(60.0*scale) as i32,(60.0*scale) as i32);
    let vertices=points.map(|p|POINT{x:p[0] as i32,y:p[1] as i32});
    let tail=CreatePolygonRgn(&vertices,WINDING);
    if window.label()=="thought" {
      for (fraction,radius) in [(0.35,6.0),(0.85,3.0)] {
        let bx=(points[0][0]+points[2][0])/2.0;let by=(points[0][1]+points[2][1])/2.0;
        let x=bx+(points[1][0]-bx)*fraction;let y=by+(points[1][1]-by)*fraction;let r=radius*scale;
        let circle=CreateEllipticRgn((x-r) as i32,(y-r) as i32,(x+r) as i32,(y+r) as i32);
        let _=CombineRgn(Some(body),Some(body),Some(circle),RGN_OR);let _=DeleteObject(circle.into());
      }
    } else {let _=CombineRgn(Some(body),Some(body),Some(tail),RGN_OR);}
    let _=DeleteObject(tail.into());
    let transferred=window.hwnd().ok().map(|hwnd|SetWindowRgn(hwnd,Some(body),true)!=0).unwrap_or(false);
    if !transferred {let _=DeleteObject(body.into());}
  }
  let _=window.emit("host-geometry",json!({"rect":rect}));
  emit(&window.state::<Output>(),json!({"type":"geometry","rect":rect}));
}
#[cfg(windows)]
fn native_interaction(window: tauri::WebviewWindow, hit: i32, origin: &'static str) {
  let handle=window.app_handle().clone();
  let _=handle.run_on_main_thread(move||unsafe {
    use windows::Win32::{Foundation::{LPARAM,WPARAM},UI::{Input::KeyboardAndMouse::ReleaseCapture,WindowsAndMessaging::{SendMessageW,WM_NCLBUTTONDOWN}}};
    // SendMessage is modal here: DefWindowProc returns after the move/size loop.
    // https://learn.microsoft.com/en-us/windows/win32/winmsg/wm-entersizemove
    if let Ok(hwnd)=window.hwnd(){
      // Capture on the UI thread immediately before the modal operation: a
      // queued host geometry update is passive, never a user interaction.
      let before=native_rect(&window);
      let _=ReleaseCapture(); let _=SendMessageW(hwnd,WM_NCLBUTTONDOWN,Some(WPARAM(hit as usize)),Some(LPARAM(0)));
      let after=native_rect(&window);
      let changed=match (before,after,origin) {
        (Some(a),Some(b),"user_drag") => a.x!=b.x || a.y!=b.y,
        (Some(a),Some(b),"user_resize") => a.width!=b.width || a.height!=b.height,
        _ => false,
      };
      if changed { report_geometry(&window,origin); }
    }
  });
}
#[tauri::command]
fn shell_drag(window: tauri::WebviewWindow) {
  #[cfg(windows)] native_interaction(window, 2 /* HTCAPTION */, "user_drag");
  #[cfg(not(windows))] { let _=window.start_dragging(); }
}
#[tauri::command]
fn shell_resize(window: tauri::WebviewWindow) {
  #[cfg(windows)] {
    native_interaction(window, 17 /* HTBOTTOMRIGHT */, "user_resize");
  }
}
fn apply_geometry(app: &tauri::AppHandle, rect: &Value) {
  let Some(label) = rect.get("window").and_then(Value::as_str) else { return };
  let Some(window) = app.get_webview_window(label) else { return };
  if let Some(target)=rect.get("tail_target").and_then(Value::as_array) { if target.len()==2 {if let (Some(x),Some(y))=(target[0].as_f64(),target[1].as_f64()){if let Ok(mut targets)=app.state::<Targets>().lock(){targets.insert(label.to_string(),[x,y]);}}} }
  let number = |key| rect.get(key).and_then(Value::as_i64).map(|v| v as i32);
  if let (Some(x), Some(y)) = (number("x"), number("y")) { let _ = window.set_position(Position::Physical(PhysicalPosition::new(x, y))); }
  if let (Some(w), Some(h)) = (number("width"), number("height")) { if w > 0 && h > 0 { let _ = window.set_size(Size::Physical(PhysicalSize::new(w as u32, h as u32))); } }
  report_geometry(&window,"passive");
  if rect.get("visible").and_then(Value::as_bool) == Some(true) { let _ = window.show(); }
  if rect.get("focus").and_then(Value::as_bool) == Some(true) && label == "input" { let _ = window.set_focus(); }
  if rect.get("visible").and_then(Value::as_bool) == Some(false) { let _ = window.hide(); }
  #[cfg(windows)] unsafe {
    use windows::Win32::UI::WindowsAndMessaging::*;
    if let Ok(hwnd)=window.hwnd() {
      let style=GetWindowLongW(hwnd,GWL_STYLE) as u32;
      let ex=GetWindowLongW(hwnd,GWL_EXSTYLE) as u32;
      SetWindowLongW(hwnd,GWL_STYLE,(style & !(WS_CAPTION.0|WS_MINIMIZEBOX.0|WS_MAXIMIZEBOX.0|WS_SYSMENU.0)) as i32);
      SetWindowLongW(hwnd,GWL_EXSTYLE,((ex & !WS_EX_APPWINDOW.0)|WS_EX_TOOLWINDOW.0) as i32);
      let _=SetWindowPos(hwnd,None,0,0,0,0,SWP_NOMOVE|SWP_NOSIZE|SWP_NOZORDER|SWP_NOACTIVATE|SWP_FRAMECHANGED);
    }
  }
}

fn main() {
  let demo = std::env::args().any(|arg| arg == "--demo");
  let nonce = std::env::var("ENGRAM_NATIVE_BUBBLE_NONCE").unwrap_or_default();
  let protocol: u8 = std::env::var("ENGRAM_NATIVE_BUBBLE_PROTOCOL").ok().and_then(|v| v.parse().ok()).unwrap_or(0);
  let out: Output = Arc::new(Mutex::new(Box::new(io::stdout())));
  emit(&out, json!({"type":"hello","protocol":protocol,"nonce":nonce}));
  let reader_out = out.clone();
  tauri::Builder::default().manage(out).manage(Targets::default()).setup(move |app| {
    for window in app.webview_windows().values() {
      // Bubble surfaces are tool windows, not taskbar application windows.
      let _=window.set_skip_taskbar(true);
      let _=window.set_decorations(false);
      let _=window.set_minimizable(false);
      let _=window.set_maximizable(false);
      #[cfg(windows)] unsafe {
        use windows::Win32::UI::WindowsAndMessaging::*;
        if let Ok(hwnd)=window.hwnd() {
          let style=GetWindowLongW(hwnd,GWL_STYLE) as u32;
          SetWindowLongW(hwnd,GWL_STYLE,(style & !(WS_CAPTION.0|WS_MINIMIZEBOX.0|WS_MAXIMIZEBOX.0|WS_SYSMENU.0)) as i32);
          let ex=GetWindowLongW(hwnd,GWL_EXSTYLE) as u32;
          SetWindowLongW(hwnd,GWL_EXSTYLE,((ex & !WS_EX_APPWINDOW.0)|WS_EX_TOOLWINDOW.0) as i32);
          let _=SetWindowPos(hwnd,None,0,0,0,0,SWP_NOMOVE|SWP_NOSIZE|SWP_NOZORDER|SWP_NOACTIVATE|SWP_FRAMECHANGED);
        }
      }
      let observed=window.clone();
      window.on_window_event(move |event| { match event {
        tauri::WindowEvent::ScaleFactorChanged{..} => report_geometry(&observed,"dpi"),
        tauri::WindowEvent::Moved(_) | tauri::WindowEvent::Resized(_) => report_geometry(&observed,"passive"),
        _ => {}
      }});
    }
    if demo { let _ = app.handle().emit("host-message", json!({"state":{"demo":true,"recent":"Demo: native bubble shell","queue":[{"id":"demo-1","request_id":"demo-1","summary":"Demo queued item"}]}})); }
    let handle = app.handle().clone();
    std::thread::spawn(move || {
      let mut input=io::stdin().lock();
      loop {
        let mut line=Vec::new();
        let Ok(count)=input.by_ref().take((MAX_LINE+1) as u64).read_until(b'\n',&mut line) else {break};
        if count==0 {break;}
        if line.len()>MAX_LINE || line.last()!=Some(&b'\n') {emit(&reader_out,json!({"type":"error","code":"inbound_too_large"}));break;}
        let Ok(message) = serde_json::from_slice::<Value>(&line) else { break };
        match message.get("type").and_then(Value::as_str) {
          Some("shutdown") => { let _ = handle.exit(0); break; }
          Some("snapshot") | Some("presentation") => { let _ = handle.emit("host-message", message); }
          Some("geometry") => { if let Some(rect) = message.get("rect") {let rect=rect.clone();let h=handle.clone();let _=handle.run_on_main_thread(move||apply_geometry(&h,&rect));} }
          Some("focus_input") => { if let Some(window) = handle.get_webview_window("input") { let _=window.show(); let _=window.set_focus(); } }
          _ => {}
        }
      }
      // EOF means the owning host disappeared. Do not become an orphan.
      handle.exit(0);
    }); Ok(())
  }).invoke_handler(tauri::generate_handler![shell_ready, shell_action, shell_geometry, shell_drag, shell_resize]).run(tauri::generate_context!()).expect("native bubble shell failed");
}

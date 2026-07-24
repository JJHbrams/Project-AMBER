import { useEffect, useRef } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { WebLinksAddon } from "@xterm/addon-web-links";
import "@xterm/xterm/css/xterm.css";
import { TauriBridge } from "../ipc/tauri_bridge";

interface XTermPanelProps {
  sessionId: string;
  provider: string;
  model?: string;
  isActive: boolean;
  onStatusChange?: (status: "running" | "idle" | "stopped" | "error") => void;
  onStdoutData?: (sessionId: string, provider: string, data: string) => void;
}

export function XTermPanel({ sessionId, provider, model, isActive, onStatusChange, onStdoutData }: XTermPanelProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const fitRef = useRef<FitAddon | null>(null);
  const termRef = useRef<Terminal | null>(null);
  const ptySessionRef = useRef<string | null>(null);
  const fitAndSyncRef = useRef<(() => void) | null>(null);
  const onStatusChangeRef = useRef(onStatusChange);
  const onStdoutDataRef = useRef(onStdoutData);
  onStatusChangeRef.current = onStatusChange;
  onStdoutDataRef.current = onStdoutData;

  // useEffect: paint 이후 실행 → RAF로 CSS Grid 레이아웃 완전 확정 후 term.open() + fit()
  // 핵심: term.open()이 charHeight를 측정하므로 레이아웃이 확정된 다음 프레임에 열어야 함
  // 폰트 로드 이전에 open하면 폴백 폰트 메트릭으로 행 높이가 고정되고 리사이즈 전까지 깨진 채로 남음
  useEffect(() => {
    if (!containerRef.current) return;
    const el = containerRef.current;

    const term = new Terminal({
      theme: { background: "#0a0a0a", foreground: "#e8e8e8", cursor: "#7c4dff" },
      fontFamily: "'Cascadia Code', 'Cascadia Mono', 'Consolas', 'Courier New', monospace",
      fontSize: 14,
      cursorBlink: true,
      convertEol: true,
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.loadAddon(new WebLinksAddon());
    // refs를 open() 전에 먼저 설정 → PTY effect가 term에 핸들러를 붙일 수 있음
    fitRef.current = fit;
    termRef.current = term;

    const fitAndSync = () => {
      const container = containerRef.current;
      const terminal = termRef.current;
      const fitAddon = fitRef.current;
      if (!container || !terminal || !fitAddon || !terminal.element) return;
      if (container.clientWidth <= 0 || container.clientHeight <= 0) return;

      const proposed = fitAddon.proposeDimensions();
      if (!proposed) return;

      const cols = Math.max(2, proposed.cols);
      // Windows(Tauri WebView2 + ConPTY) 조합에서 하단 1줄 클리핑이 반복되어
      // 안전 여유 1행을 고정으로 확보한다.
      const rows = Math.max(2, proposed.rows - 1);
      if (terminal.cols !== cols || terminal.rows !== rows) {
        terminal.resize(cols, rows);
      }

      const sid = ptySessionRef.current;
      if (sid) {
        TauriBridge.resizePty(sid, cols, rows).catch(console.error);
      }
    };
    fitAndSyncRef.current = fitAndSync;

    let rafId: number | null = null;
    let resizeRafId: number | null = null;
    let settleTimer: ReturnType<typeof setTimeout> | null = null;

    // RAF: 다음 프레임에 open → 해당 시점에 CSS 레이아웃(행 높이) 확정 보장
    rafId = requestAnimationFrame(() => {
      if (!containerRef.current) return;
      term.open(containerRef.current);
      fitAndSync();
      requestAnimationFrame(() => fitAndSyncRef.current?.());
      // fonts.ready 후 한 번 더 fit() — 시스템 폰트 로드 지연 대응
      document.fonts.ready.then(() => {
        fitAndSyncRef.current?.();
        settleTimer = setTimeout(() => fitAndSyncRef.current?.(), 60);
      });
    });

    // 컨테이너 크기 변경 시 재fit (term.element 존재 = open() 완료 확인)
    const ro = new ResizeObserver(() => {
      if (resizeRafId !== null) cancelAnimationFrame(resizeRafId);
      resizeRafId = requestAnimationFrame(() => fitAndSyncRef.current?.());
    });
    ro.observe(el);

    return () => {
      if (rafId !== null) cancelAnimationFrame(rafId);
      if (resizeRafId !== null) cancelAnimationFrame(resizeRafId);
      if (settleTimer !== null) clearTimeout(settleTimer);
      ro.disconnect();
      term.dispose();
      fitRef.current = null;
      termRef.current = null;
      fitAndSyncRef.current = null;
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // useEffect: PTY 연결 (async)
  useEffect(() => {
    const term = termRef.current;
    if (!term) return;
    let spawnSyncTimer: ReturnType<typeof setTimeout> | null = null;

    const disposeData = term.onData((data) => {
      if (ptySessionRef.current) TauriBridge.writeStdin(ptySessionRef.current, data).catch(console.error);
    });
    const disposeResize = term.onResize(({ cols, rows }) => {
      if (ptySessionRef.current) TauriBridge.resizePty(ptySessionRef.current, cols, rows).catch(console.error);
    });

    TauriBridge.spawnProvider(provider, model)
      .then((res) => {
        ptySessionRef.current = res.sessionId;
        requestAnimationFrame(() => fitAndSyncRef.current?.());
        spawnSyncTimer = setTimeout(() => fitAndSyncRef.current?.(), 50);
      })
      .catch((err) => term.writeln(`\r\n[Error] ${err}`));

    const unlistenStdout = TauriBridge.onStdout((e) => {
      if (e.sessionId === ptySessionRef.current) {
        term.write(e.data);
        onStdoutDataRef.current?.(sessionId, provider, e.data);
      }
    });
    const unlistenStatus = TauriBridge.onStatus((e) => {
      if (e.sessionId === ptySessionRef.current) onStatusChangeRef.current?.(e.status);
    });

    return () => {
      disposeData.dispose();
      disposeResize.dispose();
      unlistenStdout.then((f) => f());
      unlistenStatus.then((f) => f());
      if (spawnSyncTimer !== null) clearTimeout(spawnSyncTimer);
      if (ptySessionRef.current) TauriBridge.killProvider(ptySessionRef.current).catch(() => {});
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // display:none → display:block 전환 시 re-fit (term.element 존재 = open() 완료 확인)
  useEffect(() => {
    if (!isActive || !containerRef.current || !termRef.current?.element) return;
    const el = containerRef.current;
    const fitWhenReady = () => {
      if (el.clientWidth > 0 && el.clientHeight > 0) {
        fitAndSyncRef.current?.();
      } else {
        requestAnimationFrame(fitWhenReady);
      }
    };
    requestAnimationFrame(fitWhenReady);
  }, [isActive]);

  return (
    <div
      ref={containerRef}
      style={{
        position: "absolute",
        inset: 0,
        display: isActive ? "block" : "none",
        background: "#0a0a0a",
        overflow: "hidden",
      }}
    />
  );
}

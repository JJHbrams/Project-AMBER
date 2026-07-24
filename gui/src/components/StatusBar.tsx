import type { StatusSnapshot } from "../types/ui";

interface StatusBarProps {
  status: StatusSnapshot;
}

const STATE_COLORS: Record<StatusSnapshot["processState"], string> = {
  running: "var(--status-ok)",
  idle: "var(--text-secondary)",
  stopped: "var(--text-secondary)",
  error: "var(--status-error)",
};

export function StatusBar({ status }: StatusBarProps) {
  return (
    <footer className="status-bar">
      <span
        className="status-bar__dot"
        style={{ background: STATE_COLORS[status.processState] }}
      />
      <span className="status-bar__provider">
        {status.provider} / {status.model}
      </span>
      <span className="status-bar__sep">|</span>
      <span
        className="status-bar__mcp"
        style={{ color: status.mcpConnected ? "var(--status-ok)" : "var(--status-warn)" }}
      >
        MCP {status.mcpConnected ? "●" : "○"}
      </span>
      <span className="status-bar__sep">|</span>
      <span className="status-bar__kg">KG {status.kgNodeCount} nodes</span>
      {status.quota && (
        <>
          <span className="status-bar__sep">|</span>
          <span className="status-bar__quota">{status.quota}</span>
        </>
      )}
    </footer>
  );
}

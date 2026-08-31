import { useEffect, useMemo, useState } from "react";
import type { ProviderInfo, ProviderRuntimeInfo, SessionSummary } from "../types/ui";
import { TauriBridge, type SystemInfo } from "../ipc/tauri_bridge";

interface ProviderBannerProps {
  provider: ProviderInfo | null;
  sessions?: SessionSummary[];
  runtimeInfo?: ProviderRuntimeInfo;
  configuredModel?: string;
  mcpConnected?: boolean;
}

const FALLBACK_SYS: SystemInfo = { username: "user", home: "~", cwd: "~" };
const OLLAMA_LLAMA_ART = ` ▗▖ ▗▖
 ▟██▙
  ▐█▌
 ▗████▖
 ▐████▌
  ▐▌ ▐▌`;

const CLAUDE_OLLAMA_ART = `  ▗▖ ▗▖
  ▟██▙  ▐▛█▜▌
   ▐█▌  ▝▜█▛▘
  ▗████▖  ▝▘
   ▐▌  ▐▌`;

function useSystemInfo() {
  const [info, setInfo] = useState<SystemInfo>(FALLBACK_SYS);
  useEffect(() => {
    TauriBridge.getSystemInfo().then(setInfo).catch(() => {});
  }, []);
  return info;
}

function valueOrNA(v?: string) {
  return v && v.trim().length > 0 ? v : "N/A";
}

function formatTime(ts?: number) {
  if (!ts) return "N/A";
  try {
    return new Date(ts).toLocaleString();
  } catch {
    return "N/A";
  }
}

function RightPanel({
  provider,
  runtimeInfo,
  configuredModel,
  mcpConnected,
  recentActivity,
}: {
  provider: ProviderInfo;
  runtimeInfo?: ProviderRuntimeInfo;
  configuredModel?: string;
  mcpConnected?: boolean;
  recentActivity?: string;
}) {
  const modelText = runtimeInfo?.model?.trim()
    ? runtimeInfo.model
    : configuredModel
      ? `${configuredModel} (session configured)`
      : "N/A";

  const authText = provider.id === "antigravity" ? valueOrNA(runtimeInfo?.auth) : "N/A";

  const planText = provider.id === "ollama" || provider.id === "claude-code-ollama"
    ? "Local"
    : runtimeInfo?.plan?.trim()
      ? runtimeInfo.plan
      : provider.id === "copilot"
        ? valueOrNA(provider.planType)
        : "N/A";

  return (
    <div className="pb-box__right">
      <div className="pb-section-title">Live runtime metadata</div>
      <div className="pb-runtime-grid">
        <div className="pb-section-text"><span className="pb-cmd">Version</span> {valueOrNA(runtimeInfo?.version)}</div>
        <div className="pb-section-text"><span className="pb-cmd">Auth</span> {authText}</div>
        <div className="pb-section-text"><span className="pb-cmd">Plan</span> {planText}</div>
        <div className="pb-section-text"><span className="pb-cmd">Model</span> {modelText}</div>
      </div>
      <div className="pb-section-text">Updated: {formatTime(runtimeInfo?.updatedAt)}</div>

      <div className="pb-hdiv" />
      <div className="pb-section-title">Session</div>
      <div className="pb-section-text">{recentActivity ?? "No session activity yet."}</div>

      <div className="pb-hdiv" />
      <div className="pb-section-title">Backend</div>
      <div className="pb-section-text">
        {mcpConnected
          ? <span style={{ color: "var(--status-ok)" }}>● MCP connected</span>
          : <span style={{ color: "var(--status-error)" }}>● MCP offline</span>}
      </div>
    </div>
  );
}

function ProviderShell({
  p,
  sys,
  runtimeInfo,
  configuredModel,
  mcpConnected,
  icon,
  headline,
  sessions,
}: {
  p: ProviderInfo;
  sys: SystemInfo;
  runtimeInfo?: ProviderRuntimeInfo;
  configuredModel?: string;
  mcpConnected?: boolean;
  icon: string;
  headline: string;
  sessions: SessionSummary[];
}) {
  const recentActivity = useMemo(() => {
    const hit = sessions
      .filter((s) => s.provider === p.id)
      .sort((a, b) => b.lastActive - a.lastActive)[0];
    if (!hit) return "No recent sessions.";
    return `${hit.title} (${formatTime(hit.lastActive)})`;
  }, [sessions, p.id]);

  return (
    <div className="pb-box" style={{ "--pb-accent": p.accentColor } as React.CSSProperties}>
      <div className="pb-box__titlebar">{`-- ${p.displayName} -- live metadata only`}</div>
      <div className="pb-box__content">
        <div className="pb-box__left">
          <div className="pb-greeting">{headline}</div>
          <pre className="pb-ascii">{icon}</pre>
          <div className="pb-meta">{`Provider: ${p.displayName}`}</div>
          <div className="pb-cwd">&gt; {sys.cwd}</div>
        </div>
        <div className="pb-vdiv" />
        <RightPanel
          provider={p}
          runtimeInfo={runtimeInfo}
          configuredModel={configuredModel}
          mcpConnected={mcpConnected}
          recentActivity={recentActivity}
        />
      </div>
    </div>
  );
}

export function ProviderBanner({ provider, sessions = [], runtimeInfo, configuredModel, mcpConnected }: ProviderBannerProps) {
  const sys = useSystemInfo();

  if (!provider) {
    return (
      <div className="pb-box pb-box--empty">
        <span className="pb-dim">Select a provider to begin</span>
      </div>
    );
  }

  switch (provider.id) {
    case "copilot":
      return (
        <ProviderShell
          p={provider}
          sys={sys}
          runtimeInfo={runtimeInfo}
          configuredModel={configuredModel}
          mcpConnected={mcpConnected}
          sessions={sessions}
          headline="Describe a task to get started."
          icon={`  ╭─╮╭─╮\n  ╰─╯╰─╯\n  █ ▘▝ █\n   ▔▔▔▔`}
        />
      );
    case "claude-code":
      return (
        <ProviderShell
          p={provider}
          sys={sys}
          runtimeInfo={runtimeInfo}
          configuredModel={configuredModel}
          mcpConnected={mcpConnected}
          sessions={sessions}
          headline={`Welcome back, ${sys.username}!`}
          icon={`   ▐▛███▜▌\n  ▝▜█████▛▘\n    ▘▘ ▝▝`}
        />
      );
    case "claude-code-ollama":
      return (
        <ProviderShell
          p={provider}
          sys={sys}
          runtimeInfo={runtimeInfo}
          configuredModel={configuredModel}
          mcpConnected={mcpConnected}
          sessions={sessions}
          headline={`Claude Code + Ollama backend (${sys.username})`}
          icon={CLAUDE_OLLAMA_ART}
        />
      );
    case "antigravity":
      return (
        <ProviderShell
          p={provider}
          sys={sys}
          runtimeInfo={runtimeInfo}
          configuredModel={configuredModel}
          mcpConnected={mcpConnected}
          sessions={sessions}
          headline={`Welcome, ${sys.username}!`}
          icon={` ▝▜▄\n   ▝▜▄\n  ▗▟▀\n ▝▀`}
        />
      );
    case "ollama":
      return (
        <ProviderShell
          p={provider}
          sys={sys}
          runtimeInfo={runtimeInfo}
          configuredModel={configuredModel}
          mcpConnected={mcpConnected}
          sessions={sessions}
          headline="Ollama local runtime"
          icon={OLLAMA_LLAMA_ART}
        />
      );
    default:
      return (
        <ProviderShell
          p={provider}
          sys={sys}
          runtimeInfo={runtimeInfo}
          configuredModel={configuredModel}
          mcpConnected={mcpConnected}
          sessions={sessions}
          headline={provider.displayName}
          icon={provider.logoChar ?? "•"}
        />
      );
  }
}

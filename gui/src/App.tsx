import { useCallback, useEffect, useRef, useState } from "react";
import "./styles/theme.css";
import "./App.css";
import { LeftPanel } from "./components/LeftPanel";
import { PROVIDERS } from "./constants/providers";
import { ProviderBanner } from "./components/ProviderBanner";
import { XTermPanel } from "./components/XTermPanel";
import { KGGraphPanel } from "./components/KGGraphPanel";
import { StatusBar } from "./components/StatusBar";
import { SlidePanel } from "./components/SlidePanel";
import { TauriBridge } from "./ipc/tauri_bridge";
import type { ProviderInfo, ProviderRuntimeInfo, SessionSummary, StatusSnapshot } from "./types/ui";

const MCP_PING_INTERVAL = 5000;
const ANSI_RE = /\u001b\[[0-?]*[ -/]*[@-~]/g;
const OSC_RE = /\u001b\][^\u0007]*(\u0007|\u001b\\)/g;

function sanitizeTerminalLine(line: string): string {
  return line
    .replace(OSC_RE, "")
    .replace(ANSI_RE, "")
    .replace(/\r/g, "")
    .trim();
}

function extractRuntimePatch(providerId: string, text: string): { patch: Partial<ProviderRuntimeInfo>; facts: string[] } {
  const patch: Partial<ProviderRuntimeInfo> = {};
  const facts: string[] = [];

  const pushFact = (value?: string) => {
    if (!value) return;
    const normalized = value.trim();
    if (!normalized) return;
    facts.push(normalized);
  };

  const capture = (re: RegExp) => {
    const m = text.match(re);
    return m?.[1]?.trim();
  };

  if (providerId === "copilot") {
    const v = capture(/GitHub Copilot v([0-9.]+)/i);
    if (v) {
      patch.version = v;
      pushFact(`GitHub Copilot v${v}`);
    }
  } else if (providerId === "claude-code" || providerId === "claude-code-ollama") {
    const v = capture(/Claude Code v([0-9.]+)/i);
    if (v) {
      patch.version = v;
      pushFact(`Claude Code v${v}`);
    }
  } else if (providerId === "antigravity") {
    const v = capture(/(?:Antigravity|agy) v?([0-9.]+)/i);
    if (v) {
      patch.version = v;
      pushFact(`Antigravity v${v}`);
    }
  } else if (providerId === "ollama") {
    const v = capture(/ollama version(?: is|:)?\s*([^\s\r\n]+)/i);
    if (v) {
      patch.version = v;
      pushFact(`ollama version ${v}`);
    }
  }

  const authLine = capture(/Signed in with\s+([^\r\n]+)/i);
  if (authLine) {
    patch.auth = authLine;
    pushFact(`Signed in with ${authLine}`);
  }

  const planLine = capture(/Plan:\s*([^\r\n]+)/i);
  if (planLine) {
    patch.plan = planLine;
    pushFact(`Plan: ${planLine}`);
  }

  if (!patch.plan) {
    const tier = capture(/\b((?:Free|Pro|Max|Team|Enterprise|Business)\s+Tier)\b/i)
      ?? capture(/\b(API Usage Billing)\b/i)
      ?? capture(/\b(Individual Org)\b/i)
      ?? capture(/\b(Pro Plan|Free Plan|Team Plan|Max Plan)\b/i);
    if (tier) patch.plan = tier;
  }

  const model =
    capture(/Model:\s*([^\r\n]+)/i) ??
    capture(/Using model[:\s]+([^\r\n]+)/i) ??
    capture(/\b(claude-[a-z0-9.-]+|gpt-[a-z0-9.-]+|gemini-[a-z0-9.-]+)\b/i) ??
    capture(/\b(Sonnet\s*[0-9.]+|Haiku\s*[0-9.]+|Opus\s*[0-9.]+)\b/i) ??
    capture(/\b(llama[0-9.:_-]*|qwen[0-9.:_-]*|mistral[0-9.:_-]*|phi[0-9.:_-]*)\b/i);
  if (model) {
    patch.model = model;
    pushFact(`Model: ${model}`);
  }

  return { patch, facts };
}

function fallbackSessionModel(providerId: string, sessionModel?: string) {
  if (!sessionModel) return undefined;
  if (
    providerId === "claude-code" ||
    providerId === "claude-code-ollama" ||
    providerId === "copilot" ||
    providerId === "antigravity" ||
    providerId === "ollama"
  ) {
    return sessionModel;
  }
  return undefined;
}

function mergeUniqueFront(values: string[], existing: string[]) {
  return [...values, ...existing].filter((v, i, arr) => arr.indexOf(v) === i).slice(0, 6);
}

const DEFAULT_STATUS: StatusSnapshot = {
  provider: "—",
  model: "—",
  processState: "stopped",
  mcpConnected: false,
  kgNodeCount: 0,
};

function App() {
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [selectedProvider, setSelectedProvider] = useState<ProviderInfo | null>(null);
  const [status, setStatus] = useState<StatusSnapshot>(DEFAULT_STATUS);
  const [providerRuntime, setProviderRuntime] = useState<Record<string, ProviderRuntimeInfo>>({});
  const [slideOpen, setSlideOpen] = useState(false);
  const [slideTitle, setSlideTitle] = useState("Panel");
  const stdoutTailRef = useRef<Record<string, string>>({});
  const sessionsRef = useRef<SessionSummary[]>([]);

  useEffect(() => {
    sessionsRef.current = sessions;
  }, [sessions]);

  // MCP 연결 상태 폴링
  useEffect(() => {
    let cancelled = false;
    const ping = async () => {
      try {
        const ok = await TauriBridge.pingMcp();
        if (!cancelled) setStatus((prev) => ({ ...prev, mcpConnected: ok }));
      } catch {
        if (!cancelled) setStatus((prev) => ({ ...prev, mcpConnected: false }));
      }
    };
    ping();
    const id = setInterval(ping, MCP_PING_INTERVAL);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  const createSession = (p: ProviderInfo, prevLen: number): SessionSummary => ({
    id: `session-${Date.now()}`,
    provider: p.id,
    model: p.defaultModel,
    title: `${p.displayName} ${prevLen + 1}`,
    createdAt: Date.now(),
    lastActive: Date.now(),
  });

  const handleProviderChange = (p: ProviderInfo) => {
    setSelectedProvider(p);
    // Only show banner. User clicks "+ New session" to actually start.
  };

  const handleSessionNew = () => {
    if (!selectedProvider) return;
    const s = createSession(selectedProvider, sessions.length);
    setSessions((prev) => [s, ...prev]);
    setActiveSessionId(s.id);
    setProviderRuntime((prev) => ({
      ...prev,
      [selectedProvider.id]: {
        ...prev[selectedProvider.id],
        model: prev[selectedProvider.id]?.model ?? s.model,
        rawFacts: prev[selectedProvider.id]?.rawFacts ?? [],
        updatedAt: Date.now(),
      },
    }));
  };

  const handleSessionSelect = (id: string) => {
    setActiveSessionId(id);
    const session = sessions.find((s) => s.id === id);
    if (session) {
      const provider = PROVIDERS.find((p) => p.id === session.provider);
      if (provider) setSelectedProvider(provider);
    }
  };

  const handleProviderStdout = useCallback((sessionId: string, providerId: string, data: string) => {
    const combined = (stdoutTailRef.current[sessionId] ?? "") + data;
    const normalized = combined.replace(/\r\n/g, "\n");
    const lines = normalized.split("\n");
    const tail = lines.pop() ?? "";
    stdoutTailRef.current[sessionId] = tail.slice(-512);

    const sanitizedText = sanitizeTerminalLine([...lines, tail].join("\n"));
    const { patch: mergedPatch, facts } = extractRuntimePatch(providerId, sanitizedText);

    if (Object.keys(mergedPatch).length === 0 && facts.length === 0) return;

    setProviderRuntime((prev) => {
      const current = prev[providerId] ?? { rawFacts: [], updatedAt: Date.now() };
      const sessionModel = sessionsRef.current.find((s) => s.id === sessionId)?.model;
      const modelFallback = fallbackSessionModel(providerId, sessionModel);
      const rawFacts = mergeUniqueFront(facts, current.rawFacts);
      return {
        ...prev,
        [providerId]: {
          ...current,
          ...mergedPatch,
          model: mergedPatch.model ?? current.model ?? modelFallback,
          rawFacts,
          updatedAt: Date.now(),
        },
      };
    });
  }, []);

  const bannerSession = selectedProvider
    ? (
      sessions.find((s) => s.id === activeSessionId && s.provider === selectedProvider.id)
      ?? sessions.find((s) => s.provider === selectedProvider.id)
    )
    : undefined;

  return (
    <div className="app-grid">
      <LeftPanel
        sessions={sessions}
        activeSessionId={activeSessionId}
        selectedProvider={selectedProvider}
        onSessionSelect={handleSessionSelect}
        onSessionNew={handleSessionNew}
        onProviderChange={handleProviderChange}
        onSettingsOpen={() => {
          setSlideTitle("Settings");
          setSlideOpen(true);
        }}
      />

      <div className="right-pane">
        <ProviderBanner
          provider={selectedProvider}
          sessions={sessions}
          runtimeInfo={selectedProvider ? providerRuntime[selectedProvider.id] : undefined}
          configuredModel={bannerSession?.model}
          mcpConnected={status.mcpConnected}
        />
        <div className="mid-row">
          <div className="xterm-stack">
            {sessions.length === 0 ? (
              <div className="xterm-placeholder">
                <p>Select a provider to start</p>
                <p className="text-secondary">Click a provider in the left panel</p>
              </div>
            ) : (
              sessions.map((s) => (
                <XTermPanel
                  key={s.id}
                  sessionId={s.id}
                  provider={s.provider}
                  model={s.model}
                  isActive={s.id === activeSessionId}
                  onStatusChange={s.id === activeSessionId
                    ? (processState) => setStatus((prev) => ({
                        ...prev,
                        processState,
                        provider: selectedProvider?.displayName ?? prev.provider,
                        model: selectedProvider?.defaultModel ?? prev.model,
                      }))
                    : undefined
                  }
                  onStdoutData={handleProviderStdout}
                />
              ))
            )}
          </div>
          <KGGraphPanel
            onNodeCount={(count) => setStatus((prev) => ({ ...prev, kgNodeCount: count }))}
          />
        </div>

        <StatusBar status={status} />
      </div>

      <SlidePanel open={slideOpen} title={slideTitle} onClose={() => setSlideOpen(false)}>
        <p className="text-secondary">Phase 7에서 구현</p>
      </SlidePanel>
    </div>
  );
}

export default App;

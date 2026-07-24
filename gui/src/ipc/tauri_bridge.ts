import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import type { ProviderStdoutEvent, ProviderStatusEvent, ProviderExitEvent } from "../types/events";
import type { GraphData, SGStats } from "../types/kg";

export interface SpawnProviderResponse { sessionId: string; }
export interface SystemInfo { username: string; home: string; cwd: string; }

export const TauriBridge = {
  spawnProvider: (provider: string, model?: string) =>
    invoke<SpawnProviderResponse>("spawn_provider", { provider, model }),

  writeStdin: (sessionId: string, data: string) =>
    invoke<void>("write_stdin", { sessionId, data }),

  killProvider: (sessionId: string) =>
    invoke<void>("kill_provider", { sessionId }),

  getSystemInfo: () =>
    invoke<SystemInfo>("get_system_info"),

  pingMcp: () =>
    invoke<boolean>("ping_mcp"),

  fetchKgGraph: () =>
    invoke<GraphData>("fetch_kg_graph"),

  getKgStats: () =>
    invoke<SGStats>("get_kg_stats"),

  mcpQuery: (tool: string, params: Record<string, unknown>) =>
    invoke<unknown>("mcp_query", { tool, params }),

  resizePty: (sessionId: string, cols: number, rows: number) =>
    invoke<void>("resize_pty", { sessionId, cols, rows }),

  onStdout: (cb: (e: ProviderStdoutEvent) => void) =>
    listen<ProviderStdoutEvent>("provider://stdout", (e) => cb(e.payload)),

  onStatus: (cb: (e: ProviderStatusEvent) => void) =>
    listen<ProviderStatusEvent>("provider://status", (e) => cb(e.payload)),

  onExit: (cb: (e: ProviderExitEvent) => void) =>
    listen<ProviderExitEvent>("provider://exit", (e) => cb(e.payload)),
};

export interface ProviderStdoutEvent { sessionId: string; data: string; }
export interface ProviderStatusEvent { sessionId: string; status: "running" | "idle" | "stopped" | "error"; }
export interface ProviderExitEvent { sessionId: string; code: number | null; }

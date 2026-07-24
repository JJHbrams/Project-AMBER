export interface ProviderInfo {
  id: string;
  displayName: string;
  accentColor: string;
  planType: string;
  defaultModel: string;
  logoChar?: string;
  logoSrc?: string;
  secondaryLogoSrc?: string;
  contextWindow?: string;
  rateLimit?: string;
  features?: string[];
  quotaNote?: string;
}

export interface SessionSummary {
  id: string;
  provider: string;
  model: string;
  title: string;
  createdAt: number;
  lastActive: number;
}

export interface StatusSnapshot {
  provider: string;
  model: string;
  processState: "running" | "idle" | "stopped" | "error";
  mcpConnected: boolean;
  kgNodeCount: number;
  quota?: string;
}

export interface ProviderRuntimeInfo {
  version?: string;
  auth?: string;
  plan?: string;
  model?: string;
  rawFacts: string[];
  updatedAt: number;
}

import type { ProviderInfo } from "../types/ui";
import copilotIcon from "../assets/provider-icons/copilot.png";
import claudeIcon from "../assets/provider-icons/claude.png";
import ollamaIcon from "../assets/provider-icons/ollama.png";

export const PROVIDERS: ProviderInfo[] = [
  {
    id: "copilot",
    displayName: "GitHub Copilot",
    accentColor: "#238636",
    planType: "Pro",
    defaultModel: "claude-sonnet-4.6",
    logoSrc: copilotIcon,
    contextWindow: "200K tokens",
    rateLimit: "Completions: unlimited · Premium: 300 req/mo",
    features: [
      "Unlimited code completions",
      "Agent & chat sessions",
      "MCP tool support",
      "Multi-model: Claude / GPT / Gemini",
    ],
    quotaNote: "github.com/features/copilot",
  },
  {
    id: "claude-code",
    displayName: "Claude Code",
    accentColor: "#d97757",
    planType: "Pro",
    defaultModel: "claude-sonnet-4.6",
    logoSrc: claudeIcon,
    contextWindow: "200K tokens",
    rateLimit: "Pro: ~50 msg/5h · Haiku unlimited",
    features: [
      "/memory  /compact  /clear  /cost",
      "Bash · Edit · MultiEdit · Read",
      "MCP server support",
      "Max plan: higher rate limits",
    ],
    quotaNote: "anthropic.com/claude-code",
  },
  {
    id: "claude-code-ollama",
    displayName: "Claude Code (Ollama)",
    accentColor: "#a855f7",
    planType: "Local",
    defaultModel: "qwen3.5:4b",
    logoSrc: claudeIcon,
    secondaryLogoSrc: ollamaIcon,
    contextWindow: "model-dependent",
    rateLimit: "local inference",
    features: [
      "Claude Code UI/flow with local Ollama model",
      "No cloud billing",
      "Model can be switched per session",
    ],
    quotaNote: "ollama local backend",
  },
  {
    id: "gemini",
    displayName: "Gemini CLI",
    accentColor: "#4285f4",
    planType: "Free",
    defaultModel: "gemini-2.5-pro",
    logoChar: "✦",
    contextWindow: "1M tokens",
    rateLimit: "15 RPM · 1500 RPD · 1M TPM",
    features: ["gemini-2.5-pro (free w/ Google account)", "Google Search grounding", "Code execution sandbox", "Multimodal (image/audio) input"],
    quotaNote: "aistudio.google.com/apikey",
  },
  {
    id: "ollama",
    displayName: "Ollama",
    accentColor: "#7c4dff",
    planType: "Local",
    defaultModel: "llama3",
    logoSrc: ollamaIcon,
    contextWindow: "model-dependent",
    rateLimit: "unlimited (local inference)",
    features: ["/set system <prompt>", "/show info  /show modelfile", "/list  /load  /save  /help"],
    quotaNote: "http://localhost:11434",
  },
];

export const DEFAULT_PROVIDER = PROVIDERS[0];

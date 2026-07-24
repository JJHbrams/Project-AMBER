import type { SessionSummary, ProviderInfo } from "../types/ui";
import { PROVIDERS } from "../constants/providers";

interface LeftPanelProps {
  sessions: SessionSummary[];
  activeSessionId: string | null;
  selectedProvider: ProviderInfo | null;
  onSessionSelect: (id: string) => void;
  onSessionNew: () => void;
  onProviderChange: (provider: ProviderInfo) => void;
  onSettingsOpen: () => void;
}

export function LeftPanel({
  sessions,
  activeSessionId,
  selectedProvider,
  onSessionSelect,
  onSessionNew,
  onProviderChange,
  onSettingsOpen,
}: LeftPanelProps) {
  return (
    <aside className="left-panel">
      <div className="left-panel__header">
        <span className="left-panel__title">Provider</span>
      </div>
      <div className="provider-list">
        {PROVIDERS.map((p) => (
          <button
            key={p.id}
            className={`provider-item${selectedProvider?.id === p.id ? " provider-item--active" : ""}`}
            style={{
              borderLeftColor: selectedProvider?.id === p.id ? p.accentColor : undefined,
              ["--pb-accent" as string]: p.accentColor,
            }}
            onClick={() => onProviderChange(p)}
            aria-label={p.displayName}
            type="button"
          >
            <span className="provider-item__icon-outer">
              <span className="provider-item__icon-wrap">
                {p.logoSrc ? (
                  <img className="provider-item__icon" src={p.logoSrc} alt="" aria-hidden="true" />
                ) : (
                  <span className="provider-item__icon-fallback" style={{ color: p.accentColor }}>
                    {p.logoChar ?? p.displayName[0]}
                  </span>
                )}
              </span>
              {p.secondaryLogoSrc && (
                <img className="provider-item__icon--secondary" src={p.secondaryLogoSrc} alt="" aria-hidden="true" />
              )}
            </span>
            <span>{p.displayName}</span>
          </button>
        ))}
      </div>
      <div style={{ borderTop: "1px solid var(--border)", margin: "12px 0 0 0" }} />
      <div className="left-panel__header" style={{ marginTop: 0 }}>
        <span className="left-panel__title">Sessions</span>
        <button
          className="icon-btn"
          onClick={onSessionNew}
          title={selectedProvider ? "New session" : "Select a provider first"}
          disabled={!selectedProvider}
          style={!selectedProvider ? { opacity: 0.4, cursor: "not-allowed" } : {}}
        >
          +
        </button>
      </div>
      <nav className="left-panel__sessions">
        {sessions.length === 0 ? (
          <p className="left-panel__empty">No sessions yet</p>
        ) : (
          sessions.map((s) => (
            <button
              key={s.id}
              className={`session-item${s.id === activeSessionId ? " session-item--active" : ""}`}
              onClick={() => onSessionSelect(s.id)}
            >
              <span className="session-item__title">{s.title}</span>
              <span className="session-item__provider">{s.provider}</span>
            </button>
          ))
        )}
      </nav>
      <div className="left-panel__footer">
        <button
          className="icon-btn"
          onClick={onSettingsOpen}
          title="Settings"
          aria-label="Open settings"
        >
          ⚙
        </button>
      </div>
    </aside>
  );
}

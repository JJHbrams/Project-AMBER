import { useEffect, useRef } from "react";

interface SlidePanelProps {
  open: boolean;
  title: string;
  onClose: () => void;
  children: React.ReactNode;
}

export function SlidePanel({ open, title, onClose, children }: SlidePanelProps) {
  const panelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!panelRef.current) return;
    panelRef.current.style.transform = open ? "translateX(0)" : "translateX(100%)";
  }, [open]);

  return (
    <div
      ref={panelRef}
      className="slide-panel"
      style={{ transform: "translateX(100%)" }}
      aria-hidden={!open}
    >
      <div className="slide-panel__header">
        <span className="slide-panel__title">{title}</span>
        <button className="icon-btn" onClick={onClose} aria-label="Close panel">
          ✕
        </button>
      </div>
      <div className="slide-panel__content">{children}</div>
    </div>
  );
}

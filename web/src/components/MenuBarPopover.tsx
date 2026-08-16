import { ChartBar, ArrowsClockwise, Gear, ArrowSquareOut, Key, CaretDown, CaretUp } from "@phosphor-icons/react";
import { useState } from "react";

interface ProviderCapacityGroup {
  id: string;
  provider: string;
  familyId: string;
  window: string;
  capacity: string;
  status: "ok" | "warning" | "critical" | "neutral";
  secondary: ProviderCapacityGroup[];
}

interface MenuBarPopoverProps {
  updatedAt: string;
  todayTokens: string;
  coverage?: string;
  groups: ProviderCapacityGroup[];
}

function statusClass(status: string) {
  switch (status) {
    case "ok": return "status-ok";
    case "warning": return "status-warn";
    case "critical": return "status-bad";
    default: return "status-neutral";
  }
}

export default function MenuBarPopover({ updatedAt, todayTokens, coverage, groups }: MenuBarPopoverProps) {
  const [expanded, setExpanded] = useState<string | null>(null);
  return (
    <div className="menubar-popover">
      <header className="menubar-header">
        <div>
          <div className="menubar-title">UsageHub</div>
          <div className="menubar-subtitle">{updatedAt}</div>
        </div>
        <button className="menubar-icon-btn" aria-label="Refresh">
          <ArrowsClockwise size={16} />
        </button>
      </header>
      <button className="menubar-primary-btn">
        <ChartBar size={16} /> Open Main Window
      </button>
      <div className="menubar-today">
        <span className="dim">Today Token</span>
        <div className="menubar-today-value">
          <span>{todayTokens}</span>
          {coverage ? <span className="menubar-coverage">{coverage}</span> : null}
        </div>
      </div>
      <div className="menubar-capacity-header">
        <span className="menubar-section-title">Capacity</span>
        <span className="dim">Most urgent first</span>
      </div>
      <div className="menubar-provider-list">
        {groups.length === 0 ? (
          <div className="menubar-empty">No providers connected.</div>
        ) : (
          groups.map((g) => (
            <div key={g.id} className="menubar-provider">
              <button
                className="menubar-provider-row"
                onClick={() => setExpanded(expanded === g.id ? null : g.id)}
              >
                <div className={`provider-avatar provider-avatar-${g.familyId}`}>{g.provider.slice(0, 2).toUpperCase()}</div>
                <div className="menubar-provider-info">
                  <div className="menubar-provider-name">{g.provider}</div>
                  <div className="menubar-provider-window dim">{g.window}</div>
                </div>
                <span className={`status-badge ${statusClass(g.status)}`}>{g.capacity}</span>
                {g.secondary.length > 0 ? (
                  expanded === g.id ? <CaretUp size={14} /> : <CaretDown size={14} />
                ) : (
                  <span className="menubar-hover-actions" title="Open Console / Get API Key">
                    <ArrowSquareOut size={14} />
                    <Key size={14} />
                  </span>
                )}
              </button>
              {expanded === g.id && g.secondary.map((s) => (
                <div key={s.id} className="menubar-secondary-row">
                  <span className="dim">{s.window}</span>
                  <span className={`status-badge ${statusClass(s.status)}`}>{s.capacity}</span>
                </div>
              ))}
            </div>
          ))
        )}
      </div>
      <div className="menubar-footer">
        <button className="menubar-text-btn">Open Usage Details</button>
        <button className="menubar-text-btn">Data Health</button>
        <button className="menubar-text-btn"><Gear size={14} /> Settings</button>
      </div>
    </div>
  );
}

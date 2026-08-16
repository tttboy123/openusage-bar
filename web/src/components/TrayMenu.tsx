import { ArrowSquareOut, Gear, ArrowCounterClockwise, Prohibit } from "@phosphor-icons/react";

interface CapacityItem {
  provider: string;
  remaining: string;
  unit: string;
  ratio: number;
}

interface TrayMenuProps {
  todayTokens: string;
  urgent?: { provider: string; remaining: string; unit: string } | null;
  capacities: CapacityItem[];
}

function indicator(ratio: number) {
  if (ratio <= 0.2) return "🔴";
  if (ratio <= 0.4) return "🟡";
  return "🟢";
}

export default function TrayMenu({ todayTokens, urgent, capacities }: TrayMenuProps) {
  return (
    <div className="tray-menu">
      <div className="tray-header">UsageHub</div>
      <div className="tray-meta">
        <div className="tray-metric">
          <span className="tray-label">Today</span>
          <span className="tray-value">{todayTokens}</span>
        </div>
        {urgent ? (
          <div className="tray-metric">
            <span className="tray-label">Urgent</span>
            <span className="tray-value tray-urgent">{urgent.remaining} {urgent.unit}</span>
          </div>
        ) : null}
      </div>
      <div className="tray-items">
        {capacities.map((c) => (
          <button key={c.provider} className="tray-item">
            <span className="tray-indicator">{indicator(c.ratio)}</span>
            <span className="tray-provider">{c.provider}</span>
            <span className="tray-remaining">{c.remaining} {c.unit}</span>
          </button>
        ))}
      </div>
      <div className="tray-actions">
        <button className="tray-action"><ArrowCounterClockwise size={14} /> Refresh</button>
        <button className="tray-action"><ArrowSquareOut size={14} /> Open UsageHub</button>
        <button className="tray-action"><Gear size={14} /> Settings</button>
        <button className="tray-action tray-quit"><Prohibit size={14} /> Quit</button>
      </div>
    </div>
  );
}

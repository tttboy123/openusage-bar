import { ArrowSquareOut, Key, CloudArrowUp } from "@phosphor-icons/react";
import { type Messages, tpl } from "../i18n";
import { type SourceItem, type QuickConnectItem } from "../api";

const BRAND_COLORS: Record<string, string> = {
  deepseek: "#4D6BFE",
  moonshot: "#1A1A1A",
  minimax: "#FF6B6B",
  openai: "#10A37F",
  anthropic: "#D97757",
  google: "#4285F4",
  gemini: "#8E75B7",
  grok: "#000000",
  opencode: "#0066FF",
  openclaw: "#7C3AED",
  hermes: "#0EA5E9",
};

export type ProviderStatusKind = "ok" | "warn" | "bad" | "neutral";

export interface ProviderCardProps {
  providerId: string;
  displayName: string;
  familyId?: string;
  sourceKind?: string;
  source?: SourceItem;
  quick?: QuickConnectItem;
  t: Messages;
  isSyncing?: boolean;
}

function stateKind(state?: string): ProviderStatusKind {
  const value = (state ?? "").toLowerCase();
  if (value === "ok" || value === "available") return "ok";
  if (value === "stale") return "warn";
  if (value === "temporarily_unavailable" || value === "error") return "bad";
  return "neutral";
}

function stateLabel(t: Messages, state?: string): string {
  const value = (state ?? "").toLowerCase();
  if (value === "ok" || value === "available") return t.statusOk;
  if (value === "stale") return t.statusWarn;
  if (value === "temporarily_unavailable") return t.statusBad;
  if (value === "error") return t.statusBad;
  return t.statusNeutral;
}

function isLive(source?: SourceItem): boolean {
  if (!source?.lastSuccessAt) return false;
  const then = new Date(source.lastSuccessAt).getTime();
  if (Number.isNaN(then)) return false;
  return Date.now() - then < 5 * 60 * 1000;
}

function stateDetail(t: Messages, state?: string): string {
  const value = (state ?? "").toLowerCase();
  if (value === "ok" || value === "available") return t.healthOk;
  if (value === "stale") return t.healthStale;
  if (value === "temporarily_unavailable") return t.healthTemporarily;
  if (value === "error") return t.healthError;
  return t.comingSoon;
}

function relativeTime(date: string | null | undefined, t: Messages): string {
  if (!date) return t.never;
  const then = new Date(date).getTime();
  if (Number.isNaN(then)) return t.never;
  const minutes = Math.floor((Date.now() - then) / 60000);
  if (minutes < 1) return t.justNow ?? "just now";
  if (minutes < 60) return tpl(t.minutesAgo, { count: minutes });
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return tpl(t.hoursAgo, { count: hours });
  const days = Math.floor(hours / 24);
  return tpl(t.daysAgo, { count: days });
}

export function brandColor(familyId?: string): string {
  if (!familyId) return "var(--text-faint)";
  return BRAND_COLORS[familyId.toLowerCase()] ?? "var(--text-faint)";
}

export default function ProviderCard({
  displayName,
  familyId,
  sourceKind,
  source,
  quick,
  t,
  isSyncing,
}: ProviderCardProps) {
  const kind = stateKind(source?.state);
  const color = brandColor(familyId);
  const live = isLive(source);
  return (
    <article className="provider-card">
      <span
        className="provider-icon"
        style={{ background: color }}
        aria-hidden="true"
      >
        {(familyId ?? "").slice(0, 2).toUpperCase()}
      </span>
      <div className="provider-info">
        <h4 title={displayName}>{displayName}</h4>
        {quick?.consoleUrl ? (
          <a
            href={quick.consoleUrl}
            target="_blank"
            rel="noopener noreferrer"
            title={quick.consoleUrl}
          >
            {quick.consoleUrl.replace(/^https?:\/\//, "")}
          </a>
        ) : (
          <span className="dim">{sourceKind ?? "n/a"}</span>
        )}
      </div>
      <div
        className={`provider-status provider-status-${kind}`}
        title={`${stateDetail(t, source?.state)} · ${relativeTime(source?.lastSuccessAt, t)}`}
      >
        {isSyncing ? (
          <>
            <span className="provider-status-syncing" aria-hidden="true" />
            <span className="provider-status-text">{t.syncing}</span>
          </>
        ) : live && kind === "ok" ? (
          <>
            <CloudArrowUp size={12} aria-hidden="true" />
            <span className="provider-status-live" aria-hidden="true" />
            <span className="provider-status-text">{t.live}</span>
            <span className="provider-status-time">
              {relativeTime(source?.lastSuccessAt, t)}
            </span>
          </>
        ) : (
          <>
            <span className="provider-status-dot" aria-hidden="true" />
            <span>{stateLabel(t, source?.state)}</span>
            <span className="provider-status-time">
              {relativeTime(source?.lastSuccessAt, t)}
            </span>
          </>
        )}
      </div>
      <div className="provider-actions">
        {quick?.consoleUrl ? (
          <a
            className="icon-btn"
            href={quick.consoleUrl}
            target="_blank"
            rel="noopener noreferrer"
            aria-label={t.openConsole}
            title={t.openConsole}
          >
            <ArrowSquareOut size={16} />
          </a>
        ) : null}
        {quick?.apiKeyUrl ? (
          <a
            className="icon-btn"
            href={quick.apiKeyUrl}
            target="_blank"
            rel="noopener noreferrer"
            aria-label={t.getApiKey}
            title={t.getApiKey}
          >
            <Key size={16} />
          </a>
        ) : null}
      </div>
    </article>
  );
}

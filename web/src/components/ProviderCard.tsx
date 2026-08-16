import { ArrowSquareOut, Key, CloudArrowUp } from "@phosphor-icons/react";
import { ProviderIcon } from "./ProviderIcon";
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
  variant?: "grid" | "list";
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
  return tpl(days === 1 ? t.dayAgo : t.daysAgo, { count: days });
}

export function brandColor(familyId?: string): string {
  if (!familyId) return "var(--text-faint)";
  return BRAND_COLORS[familyId.toLowerCase()] ?? "var(--text-faint)";
}

function brandLuminance(hex: string): number {
  const value = hex.startsWith("#") ? hex.slice(1) : hex;
  const channels = [0, 2, 4].map((i) => {
    const channel = parseInt(value.slice(i, i + 2), 16) / 255;
    return channel <= 0.03928
      ? channel / 12.92
      : Math.pow((channel + 0.055) / 1.055, 2.4);
  });
  return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
}

const BRAND_DARK_TEXT = "#111114";
const BRAND_LIGHT_TEXT = "#ffffff";

function brandContrast(luminance: number, textLuminance: number): number {
  const lighter = Math.max(luminance, textLuminance);
  const darker = Math.min(luminance, textLuminance);
  return (lighter + 0.05) / (darker + 0.05);
}

/** Readable foreground for a hex avatar color: pick the higher-contrast text. */
export function brandTextColorForHex(color: string): string {
  if (!color.startsWith("#") || color.length !== 7) return BRAND_LIGHT_TEXT;
  const luminance = brandLuminance(color);
  const dark = brandContrast(luminance, brandLuminance(BRAND_DARK_TEXT));
  const light = brandContrast(luminance, brandLuminance(BRAND_LIGHT_TEXT));
  return dark >= light ? BRAND_DARK_TEXT : BRAND_LIGHT_TEXT;
}

/** Readable foreground for a brand-color avatar (dark text on light brands). */
export function brandTextColor(familyId?: string): string {
  return brandTextColorForHex(brandColor(familyId));
}

export default function ProviderCard({
  displayName,
  familyId,
  sourceKind,
  source,
  quick,
  t,
  isSyncing,
  variant = "grid",
}: ProviderCardProps) {
  const kind = stateKind(source?.state);
  const live = isLive(source);
  return (
    <article className={variant === "list" ? "provider-row" : "provider-card"}>
      <ProviderIcon
        familyId={familyId}
        name={displayName}
        size={variant === "list" ? 28 : 34}
        className={variant === "list" ? "provider-icon-sm" : ""}
      />
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

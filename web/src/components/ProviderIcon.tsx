import { brandTextColorForHex } from "./ProviderCard";

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

/**
 * Real provider brand marks bundled in web/public/icons. Family IDs without a
 * mark fall back to the brand-color initial-letter tile.
 */
const ICON_BY_FAMILY: Record<string, string> = {
  anthropic: "/icons/anthropic.svg",
  claude_code: "/icons/anthropic.svg",
  openai: "/icons/openai.svg",
  codex: "/icons/openai.svg",
  deepseek: "/icons/deepseek.svg",
  minimax: "/icons/minimax.svg",
  google: "/icons/googlegemini.svg",
  gemini_api: "/icons/googlegemini.svg",
  gemini_cli: "/icons/googlegemini.svg",
  moonshot: "/icons/kimi.svg",
  kimi_cli: "/icons/kimi.svg",
  openrouter: "/icons/openrouter.svg",
  ollama: "/icons/ollama.svg",
  opencode: "/icons/opencode.svg",
  qwen_cli: "/icons/qwen.svg",
  zed: "/icons/zedindustries.svg",
  hermes: "/icons/hermes.svg",
  cursor: "/icons/cursor.png",
  kiro_cli: "/icons/kiro.png",
};

export function providerIconPath(familyId?: string): string | null {
  if (!familyId) return null;
  return ICON_BY_FAMILY[familyId] ?? null;
}

export function ProviderIcon({
  familyId,
  name,
  size = 34,
  className = "",
}: {
  familyId?: string;
  name?: string;
  size?: number;
  className?: string;
}) {
  const path = providerIconPath(familyId);
  const label = name ?? familyId ?? "?";
  if (path) {
    return (
      <span
        className={`provider-icon-tile ${className}`}
        style={{ width: size, height: size }}
      >
        <img
          src={path}
          alt=""
          className="provider-brand-icon"
          style={{ maxWidth: size * 0.72, maxHeight: size * 0.72 }}
        />
      </span>
    );
  }
  const color = BRAND_COLORS[familyId ?? ""] ?? "var(--surface-alt)";
  const fg =
    color === "var(--surface-alt)"
      ? "var(--text)"
      : brandTextColorForHex(color);
  return (
    <span
      className={`provider-icon-tile provider-icon-fallback ${className}`}
      style={{ width: size, height: size, background: color, color: fg }}
      aria-hidden="true"
    >
      {label.slice(0, 2).toUpperCase()}
    </span>
  );
}

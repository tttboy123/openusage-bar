import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { MagnifyingGlass, X, Key, ArrowLeft } from "@phosphor-icons/react";
import { brandTextColorForHex } from "./ProviderCard";
import type { ProviderConfigPreset } from "../api";
import { type Messages, tpl } from "../i18n";

const BRAND_COLORS: Record<string, string> = {
  deepseek: "#4D6BFE",
  moonshot: "#1A1A1A",
  minimax: "#FF6B6B",
  openai: "#10A37F",
  anthropic: "#D97757",
  google: "#4285F4",
  opencode: "#0066FF",
  zai: "#4D6BFE",
  openrouter: "#843DCE",
  siliconflow: "#00B8D9",
};

const AGENT_LABELS: Record<string, string> = {
  claude_code: "Claude Code",
  codex: "Codex",
  gemini_cli: "Gemini CLI",
  opencode: "OpenCode",
};

interface ApplyResult {
  ok: boolean;
  agent?: string;
  name?: string;
  category?: string;
  status?: string;
}

interface Props {
  open: boolean;
  presets: ProviderConfigPreset[];
  onClose: () => void;
  t: Messages;
  canApply: boolean;
  apply?: (req: {
    presetId: string;
    apiKey: string;
    baseUrl?: string | null;
    model?: string | null;
  }) => Promise<ApplyResult | null>;
}

type CategoryFilter = "all" | "official" | "gateway";

export default function AddProviderDialog({
  open,
  presets,
  onClose,
  t,
  canApply,
  apply,
}: Props) {
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState<CategoryFilter>("all");
  const [selected, setSelected] = useState<ProviderConfigPreset | null>(null);
  const [apiKey, setApiKey] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [model, setModel] = useState("");
  const [showKey, setShowKey] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState(false);
  const [closing, setClosing] = useState(false);
  const searchRef = useRef<HTMLInputElement>(null);
  const keyRef = useRef<HTMLInputElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeTimer = useRef<number | null>(null);

  useEffect(() => {
    if (open) {
      setClosing(false);
      setQuery("");
      setCategory("all");
      setSelected(null);
      setApiKey("");
      setBaseUrl("");
      setModel("");
      setShowKey(false);
      setSaving(false);
      setSaved(false);
      setError(false);
    }
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const id = requestAnimationFrame(() => {
      if (selected) {
        keyRef.current?.focus();
      } else {
        searchRef.current?.focus();
      }
    });
    return () => cancelAnimationFrame(id);
  }, [open, selected]);

  const requestClose = useCallback(() => {
    if (closing) return;
    setClosing(true);
    closeTimer.current = window.setTimeout(() => {
      closeTimer.current = null;
      setClosing(false);
      onClose();
    }, 150);
  }, [closing, onClose]);

  useEffect(() => {
    if (!open) return;
    const trigger = document.activeElement as HTMLElement | null;
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        requestClose();
        return;
      }
      if (event.key !== "Tab") return;
      const dialog = dialogRef.current;
      if (!dialog) return;
      const focusables = Array.from(
        dialog.querySelectorAll<HTMLElement>(
          "button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), a[href], [tabindex]:not([tabindex=\"-1\"])",
        ),
      );
      if (focusables.length === 0) return;
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      const active = document.activeElement;
      if (event.shiftKey) {
        if (active === first || !dialog.contains(active)) {
          event.preventDefault();
          last.focus();
        }
      } else if (active === last || !dialog.contains(active)) {
        event.preventDefault();
        first.focus();
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      trigger?.focus?.();
    };
  }, [open, requestClose]);

  useEffect(() => {
    return () => {
      if (closeTimer.current !== null) {
        window.clearTimeout(closeTimer.current);
      }
    };
  }, []);

  const filtered = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    return presets.filter((preset) => {
      if (category !== "all" && preset.category !== category) return false;
      if (!normalized) return true;
      return (
        preset.name.toLowerCase().includes(normalized) ||
        preset.model.toLowerCase().includes(normalized) ||
        preset.familyId.toLowerCase().includes(normalized)
      );
    });
  }, [query, category, presets]);

  if (!open) return null;

  function choose(preset: ProviderConfigPreset) {
    setSelected(preset);
    setBaseUrl(preset.baseUrl);
    setModel(preset.model);
    setApiKey("");
    setError(false);
    setSaved(false);
  }

  async function handleSave() {
    if (!selected || !apply) return;
    const key = apiKey.trim();
    if (!key) {
      setError(true);
      return;
    }
    setSaving(true);
    setError(false);
    const result = await apply({
      presetId: selected.presetId,
      apiKey: key,
      baseUrl: baseUrl.trim() || null,
      model: model.trim() || null,
    });
    setSaving(false);
    if (result?.ok) {
      setSaved(true);
    } else {
      setError(true);
    }
  }

  const agentLabel = selected
    ? (AGENT_LABELS[selected.agent] ?? selected.agent)
    : "";

  return (
    <div
      className={`dialog-overlay${closing ? " closing" : ""}`}
      role="presentation"
      onClick={requestClose}
    >
      <div
        className={`dialog${closing ? " closing" : ""}`}
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-label={t.addProvider}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="dialog-head">
          <div>
            <h3>{t.addProvider}</h3>
            <p>{saved && selected ? tpl(t.providerConfigSavedTo, { agent: agentLabel }) : t.choosePreset}</p>
          </div>
          <button
            type="button"
            className="icon-btn"
            onClick={requestClose}
            aria-label={t.close}
          >
            <X size={16} />
          </button>
        </div>

        {!selected ? (
          <>
            <div className="preset-search">
              <MagnifyingGlass size={16} />
              <input
                ref={searchRef}
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder={t.searchPresets}
                aria-label={t.searchPresets}
              />
            </div>
            <div className="preset-category-tabs" role="tablist">
              {(
                [
                  ["all", t.providerConfigAll],
                  ["official", t.providerConfigOfficial],
                  ["gateway", t.providerConfigGateway],
                ] as const
              ).map(([value, label]) => (
                <button
                  key={value}
                  type="button"
                  role="tab"
                  aria-selected={category === value}
                  className={`preset-category-tab${category === value ? " active" : ""}`}
                  onClick={() => setCategory(value)}
                >
                  {label}
                </button>
              ))}
            </div>
            <div className="preset-grid">
              {filtered.map((preset) => (
                <button
                  type="button"
                  key={preset.presetId}
                  className="preset-card"
                  onClick={() => choose(preset)}
                >
                  <span
                    className="preset-icon"
                    style={{
                      background:
                        BRAND_COLORS[preset.familyId] ?? "var(--surface-alt)",
                      color: BRAND_COLORS[preset.familyId]
                        ? brandTextColorForHex(BRAND_COLORS[preset.familyId])
                        : "var(--text)",
                    }}
                  >
                    {preset.name.slice(0, 2).toUpperCase()}
                  </span>
                  <span className="preset-card-name">{preset.name}</span>
                  <span className={`preset-category-badge ${preset.category}`}>
                    {preset.category === "official"
                      ? t.providerConfigOfficial
                      : t.providerConfigGateway}
                  </span>
                  <span className="preset-card-meta">
                    {AGENT_LABELS[preset.agent] ?? preset.agent} · {preset.model}
                  </span>
                </button>
              ))}
              {filtered.length === 0 ? (
                <p className="empty" style={{ gridColumn: "1 / -1" }}>
                  {t.noMatchingPresets}
                </p>
              ) : null}
            </div>
          </>
        ) : (
          <div className="dialog-form">
            <div className="selected-preset" tabIndex={-1}>
              <div className="selected-preset-title">
                <span>{selected.name}</span>
                <span className={`preset-category-badge ${selected.category}`}>
                  {selected.category === "official"
                    ? t.providerConfigOfficial
                    : t.providerConfigGateway}
                </span>
                <span className="preset-card-meta">
                  {tpl(t.providerConfigAgent, { agent: agentLabel })}
                </span>
              </div>
            </div>
            <label className="field-label" htmlFor="pc-api-key">
              {t.apiKey}
            </label>
            <div className="field-row">
              <Key size={15} />
              <input
                id="pc-api-key"
                ref={keyRef}
                type={showKey ? "text" : "password"}
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                placeholder="sk-…"
                autoComplete="off"
                spellCheck={false}
              />
              <button
                type="button"
                className="icon-btn"
                onClick={() => setShowKey((v) => !v)}
                aria-label={showKey ? t.hideApiKey : t.showApiKey}
              >
                {showKey ? "🙈" : "👁"}
              </button>
            </div>
            <label className="field-label" htmlFor="pc-base-url">
              {t.providerConfigBaseUrl}
            </label>
            <input
              id="pc-base-url"
              className="field-input"
              type="text"
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              disabled={!selected.allowCustomEndpoints}
              spellCheck={false}
            />
            {!selected.allowCustomEndpoints ? (
              <p className="field-hint">{t.providerConfigEndpointFixed}</p>
            ) : null}
            <label className="field-label" htmlFor="pc-model">
              {t.providerConfigModel}
            </label>
            <input
              id="pc-model"
              className="field-input"
              type="text"
              value={model}
              onChange={(e) => setModel(e.target.value)}
              spellCheck={false}
            />
            <div className="selected-preset-links">
              {selected.apiKeyUrl ? (
                <a
                  className="btn-link"
                  href={selected.apiKeyUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  {t.getApiKey}
                </a>
              ) : null}
              {selected.consoleUrl ? (
                <a
                  className="btn-link"
                  href={selected.consoleUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  {t.openConsole}
                </a>
              ) : null}
            </div>
            {!canApply ? (
              <p className="field-hint" role="status">
                {t.providerConfigDesktopRequired}
              </p>
            ) : null}
            {error ? (
              <p className="field-error" role="alert">
                {t.providerConfigFailed}
              </p>
            ) : null}
            <div className="dialog-actions">
              <button
                type="button"
                className="icon-btn"
                onClick={() => setSelected(null)}
              >
                <ArrowLeft size={15} />
                {t.back}
              </button>
              <span style={{ flex: 1 }} />
              <button
                type="button"
                className="primary-btn"
                onClick={handleSave}
                disabled={!canApply || saving || saved}
              >
                {saving ? (
                  <>
                    <span className="spinner" aria-hidden="true" />
                    {t.providerConfigSaving}
                  </>
                ) : saved ? (
                  t.save
                ) : (
                  t.providerConfigSave
                )}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

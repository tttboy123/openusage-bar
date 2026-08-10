import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { MagnifyingGlass, X } from "@phosphor-icons/react";
import type { QuickConnectItem } from "../api";
import { type Messages } from "../i18n";

interface Props {
  open: boolean;
  presets: QuickConnectItem[];
  onClose: () => void;
  t: Messages;
}

const BRAND_COLORS: Record<string, string> = {
  deepseek: "#4D6BFE",
  moonshot: "#1A1A1A",
  minimax: "#FF6B6B",
  step_plan: "#4D6BFE",
  codex: "#10A37F",
};

const PRESET_META: Record<string, { name: string; models: string }> = {
  anthropic: { name: "Claude", models: "Claude Sonnet · Opus" },
  codex: { name: "Codex", models: "GPT-5 · Codex" },
  deepseek: { name: "DeepSeek", models: "DeepSeek V3 · R1" },
  gemini_api: { name: "Gemini API", models: "Gemini 2.5 Pro" },
  google: { name: "Gemini (Google)", models: "Gemini 2.5 Pro" },
  minimax: { name: "MiniMax", models: "MiniMax M2 · Text-01" },
  moonshot: { name: "Kimi", models: "Kimi k2" },
  openai: { name: "OpenAI", models: "GPT-5 · o-series" },
  step_plan: { name: "StepFun", models: "Step 2 · Mini" },
  cc_switch: { name: "CC Switch", models: "Auto-detect" },
  omniroute: { name: "OmniRoute", models: "Auto-detect" },
};

function presetName(preset: QuickConnectItem): string {
  return PRESET_META[preset.familyId]?.name ?? preset.familyId;
}

export default function AddProviderDialog({ open, presets, onClose, t }: Props) {
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<QuickConnectItem | null>(null);
  const [testing, setTesting] = useState(false);
  const [latency, setLatency] = useState<number | null>(null);
  const [closing, setClosing] = useState(false);
  const searchRef = useRef<HTMLInputElement>(null);
  const selectedRef = useRef<HTMLDivElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeTimer = useRef<number | null>(null);

  useEffect(() => {
    if (open) {
      setClosing(false);
      setQuery("");
      setSelected(null);
      setTesting(false);
      setLatency(null);
    }
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const id = requestAnimationFrame(() => {
      if (selected) {
        selectedRef.current?.focus();
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
          'button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), a[href], [tabindex]:not([tabindex="-1"])',
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
    if (!normalized) return presets;
    return presets.filter((preset) => {
      const name = presetName(preset).toLowerCase();
      const models = PRESET_META[preset.familyId]?.models.toLowerCase() ?? "";
      return (
        preset.familyId.toLowerCase().includes(normalized) ||
        name.includes(normalized) ||
        models.includes(normalized)
      );
    });
  }, [query, presets]);

  if (!open) return null;

  async function checkConsoleReachability() {
    if (!selected) return;
    setTesting(true);
    setLatency(null);
    const started = performance.now();
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 8000);
    try {
      await fetch(selected.consoleUrl, {
        method: "HEAD",
        mode: "no-cors",
        credentials: "omit",
        referrerPolicy: "no-referrer",
        signal: controller.signal,
      });
      setLatency(Math.round(performance.now() - started));
    } catch {
      setLatency(-1);
    } finally {
      window.clearTimeout(timeout);
      setTesting(false);
    }
  }

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
            <p>{t.choosePreset}</p>
            <p className="preset-hint web-preview-note">{t.webPreviewNote}</p>
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
            <div className="preset-grid">
              {filtered.map((preset) => {
                const meta = PRESET_META[preset.familyId];
                return (
                  <button
                    type="button"
                    key={preset.familyId}
                    className="preset-card"
                    onClick={() => {
                      setLatency(null);
                      setSelected(preset);
                    }}
                  >
                    <span
                      className="preset-icon"
                      style={{
                        background:
                          BRAND_COLORS[preset.familyId] ?? "var(--surface-alt)",
                      }}
                    >
                      {presetName(preset).slice(0, 2).toUpperCase()}
                    </span>
                    <span className="preset-card-name">{presetName(preset)}</span>
                    {meta?.models ? (
                      <span className="preset-card-meta">{meta.models}</span>
                    ) : null}
                  </button>
                );
              })}
              {filtered.length === 0 ? (
                <p className="empty" style={{ gridColumn: "1 / -1" }}>
                  {t.noMatchingPresets}
                </p>
              ) : null}
            </div>
          </>
        ) : (
          <div className="dialog-form">
            <div
              ref={selectedRef}
              className="selected-preset"
              tabIndex={-1}
            >
              <div className="selected-preset-title">
                <span>{presetName(selected)}</span>
                <span className="preset-card-meta">
                  {PRESET_META[selected.familyId]?.models ?? ""}
                </span>
              </div>
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
            </div>
            <div className="dialog-actions">
              <button
                type="button"
                className="icon-btn"
                onClick={checkConsoleReachability}
                disabled={testing}
              >
                {testing ? (
                  <>
                    <span className="spinner" aria-hidden="true" />
                    {t.checkingConsoleReachability}
                  </>
                ) : (
                  t.checkConsoleReachability
                )}
              </button>
              {latency !== null ? (
                <span className="mono dim" role="status" aria-live="polite">
                  {latency >= 0 ? `${latency}ms` : t.unavailable}
                </span>
              ) : null}
              <span style={{ flex: 1 }} />
              <button
                type="button"
                className="icon-btn"
                onClick={() => setSelected(null)}
              >
                {t.back}
              </button>
              <button type="button" className="primary-btn" onClick={requestClose}>
                {t.close}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

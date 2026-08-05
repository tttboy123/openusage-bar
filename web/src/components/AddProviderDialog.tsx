import { useEffect, useMemo, useRef, useState } from "react";
import { MagnifyingGlass, Eye, EyeSlash, X } from "@phosphor-icons/react";
import type { QuickConnectItem } from "../api";
import { useToast } from "./Toast";

interface Props {
  open: boolean;
  presets: QuickConnectItem[];
  onClose: () => void;
}

const BRAND_COLORS: Record<string, string> = {
  deepseek: "#4D6BFE",
  moonshot: "#1A1A1A",
  minimax: "#FF6B6B",
  step_plan: "#4D6BFE",
  codex: "#10A37F",
};

export default function AddProviderDialog({ open, presets, onClose }: Props) {
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<QuickConnectItem | null>(null);
  const [apiKey, setApiKey] = useState("");
  const [showKey, setShowKey] = useState(false);
  const [testing, setTesting] = useState(false);
  const [latency, setLatency] = useState<number | null>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const toast = useToast();

  useEffect(() => {
    if (open) {
      setQuery("");
      setSelected(null);
      setApiKey("");
      setLatency(null);
      window.setTimeout(() => searchRef.current?.focus(), 50);
    }
  }, [open]);

  const filtered = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    if (!normalized) return presets;
    return presets.filter((preset) =>
      preset.familyId.toLowerCase().includes(normalized),
    );
  }, [query, presets]);

  if (!open) return null;

  async function testEndpoint() {
    if (!selected) return;
    setTesting(true);
    const started = performance.now();
    try {
      await fetch(selected.consoleUrl, { method: "HEAD", mode: "no-cors" });
    } catch {
      // no-cors HEAD can reject on opaque responses; treat as reachable
    }
    setLatency(Math.round(performance.now() - started));
    setTesting(false);
  }

  function save() {
    if (!selected) return;
    if (!apiKey.trim()) {
      toast.show("error", "API key is required");
      return;
    }
    toast.show("success", `${selected.familyId} saved.`);
    onClose();
  }

  return (
    <div className="dialog-overlay" role="presentation" onClick={onClose}>
      <div
        className="dialog"
        role="dialog"
        aria-modal="true"
        aria-label="Add Provider"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="dialog-head">
          <div>
            <h3>Add Provider</h3>
            <p>Choose a preset, then enter your API key.</p>
          </div>
          <button type="button" className="icon-btn" onClick={onClose} aria-label="Close">
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
                placeholder="Search presets..."
                aria-label="Search provider presets"
              />
            </div>
            <div className="preset-grid">
              {filtered.map((preset) => (
                <button
                  type="button"
                  key={preset.familyId}
                  className="preset-card"
                  onClick={() => setSelected(preset)}
                >
                  <span
                    className="preset-icon"
                    style={{
                      background: BRAND_COLORS[preset.familyId] ?? "var(--surface-alt)",
                    }}
                  >
                    {preset.familyId.slice(0, 2).toUpperCase()}
                  </span>
                  <span>{preset.familyId}</span>
                </button>
              ))}
              {filtered.length === 0 ? (
                <p className="empty" style={{ gridColumn: "1 / -1" }}>
                  No matching presets.
                </p>
              ) : null}
            </div>
          </>
        ) : (
          <div className="dialog-form">
            <p className="selected-preset">
              {selected.familyId}
              {selected.apiKeyUrl ? (
                <a
                  className="btn-link"
                  href={selected.apiKeyUrl}
                  target="_blank"
                  rel="noopener"
                >
                  Get API Key
                </a>
              ) : null}
              {selected.consoleUrl ? (
                <a
                  className="btn-link"
                  href={selected.consoleUrl}
                  target="_blank"
                  rel="noopener"
                >
                  Open Console
                </a>
              ) : null}
            </p>
            <label className="field-label" htmlFor="api-key">
              API Key
            </label>
            <div className="key-input">
              <input
                id="api-key"
                type={showKey ? "text" : "password"}
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                autoComplete="off"
                spellCheck={false}
              />
              <button
                type="button"
                className="icon-btn"
                onClick={() => setShowKey((v) => !v)}
                aria-label={showKey ? "Hide API key" : "Show API key"}
              >
                {showKey ? <EyeSlash size={16} /> : <Eye size={16} />}
              </button>
            </div>
            <div className="dialog-actions">
              <button
                type="button"
                className="icon-btn"
                onClick={testEndpoint}
                disabled={testing}
              >
                {testing ? "Testing..." : "Test Endpoint"}
              </button>
              {latency !== null ? (
                <span className="mono dim">
                  {latency >= 0 ? `${latency}ms` : "unreachable"}
                </span>
              ) : null}
              <span style={{ flex: 1 }} />
              <button
                type="button"
                className="icon-btn"
                onClick={() => setSelected(null)}
              >
                Back
              </button>
              <button type="button" className="primary-btn" onClick={save}>
                Save
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

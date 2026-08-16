import { useEffect, useRef, useState, type RefObject } from "react";
import { Key, PencilSimple, Trash, X } from "@phosphor-icons/react";
import {
  GATEWAY_PROVIDER_PRESETS,
  type GatewayProviderPreset,
  type GatewayAccountAction,
} from "../gatewayAccountActions";
import type { Messages } from "../i18n";

export function ProviderAccountActions({
  trusted,
  busy,
  displayId,
  onAction,
  t,
}: {
  trusted: boolean;
  busy: boolean;
  displayId: string;
  onAction: (
    action: Exclude<GatewayAccountAction, "gatewayAccount.openCreate">,
    displayId: string,
  ) => void;
  t: Messages;
}) {
  if (!trusted) return null;
  return (
    <div
      className="provider-account-actions"
      role="group"
      aria-label={`${t.gatewayAccountActions}: ${displayId}`}
    >
      <button
        type="button"
        className="icon-btn"
        disabled={busy}
        onClick={() => onAction("gatewayAccount.openEdit", displayId)}
      >
        <PencilSimple size={15} aria-hidden="true" />
        {t.editGatewayAccount}
      </button>
      <button
        type="button"
        className="icon-btn"
        disabled={busy}
        onClick={() => onAction("gatewayAccount.openReplace", displayId)}
      >
        <Key size={15} aria-hidden="true" />
        {t.replaceGatewayCredential}
      </button>
      <button
        type="button"
        className="icon-btn"
        disabled={busy}
        onClick={() => onAction("gatewayAccount.openRemove", displayId)}
      >
        <Trash size={15} aria-hidden="true" />
        {t.removeGatewayAccount}
      </button>
    </div>
  );
}

export function GatewayAccountCreateDialog({
  pending,
  onCancel,
  onContinue,
  t,
}: {
  pending: boolean;
  onCancel: () => void;
  onContinue: (preset: GatewayProviderPreset) => void;
  t: Messages;
}) {
  const [preset, setPreset] = useState<GatewayProviderPreset>("openai");
  const dialogRef = useRef<HTMLDivElement>(null);
  const selectRef = useRef<HTMLSelectElement>(null);
  useDialogKeyboard(dialogRef, selectRef, pending, onCancel);
  return (
    <div
      className="dialog-overlay"
      role="presentation"
      onMouseDown={pending ? undefined : onCancel}
    >
      <div
        ref={dialogRef}
        className="dialog gateway-account-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="gateway-account-create-title"
        aria-describedby="gateway-account-create-description"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="dialog-head">
          <div>
            <h3 id="gateway-account-create-title">{t.addGatewayAccount}</h3>
            <p id="gateway-account-create-description">{t.gatewayAccountSecureWindow}</p>
          </div>
          <button
            type="button"
            className="icon-btn"
            aria-label={t.close}
            onClick={onCancel}
            disabled={pending}
          >
            <X size={16} aria-hidden="true" />
          </button>
        </div>
        <label className="account-pool-field">
          <span>{t.chooseGatewayProvider}</span>
          <select
            ref={selectRef}
            className="filter-select"
            value={preset}
            onChange={(event) => setPreset(event.target.value as GatewayProviderPreset)}
            disabled={pending}
          >
            {GATEWAY_PROVIDER_PRESETS.map((value) => (
              <option key={value} value={value}>{gatewayProviderName(value)}</option>
            ))}
          </select>
        </label>
        <p className="gateway-account-privacy-note">{t.gatewayAccountNoCredentialFields}</p>
        <div className="account-pool-dialog-actions">
          <button type="button" className="secondary-btn" onClick={onCancel} disabled={pending}>
            {t.cancel}
          </button>
          <button
            type="button"
            className="primary-btn"
            onClick={() => onContinue(preset)}
            disabled={pending}
          >
            {pending ? t.openingSecureWindow : t.continueSecureWindow}
          </button>
        </div>
      </div>
    </div>
  );
}

function gatewayProviderName(preset: GatewayProviderPreset): string {
  if (preset === "openai") return "OpenAI";
  if (preset === "anthropic") return "Anthropic";
  if (preset === "deepseek") return "DeepSeek";
  return "OpenRouter";
}

function useDialogKeyboard<T extends HTMLElement>(
  dialogRef: RefObject<HTMLDivElement | null>,
  initialFocusRef: RefObject<T | null>,
  pending: boolean,
  onCancel: () => void,
) {
  const pendingRef = useRef(pending);
  const onCancelRef = useRef(onCancel);
  pendingRef.current = pending;
  onCancelRef.current = onCancel;
  useEffect(() => {
    const trigger = document.activeElement as HTMLElement | null;
    const frame = requestAnimationFrame(() => initialFocusRef.current?.focus());
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        if (!pendingRef.current) onCancelRef.current();
        return;
      }
      if (event.key !== "Tab") return;
      const dialog = dialogRef.current;
      if (!dialog) return;
      const focusable = Array.from(
        dialog.querySelectorAll<HTMLElement>(
          'button:not(:disabled), select:not(:disabled), [tabindex]:not([tabindex="-1"])',
        ),
      );
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => {
      cancelAnimationFrame(frame);
      window.removeEventListener("keydown", onKeyDown);
      trigger?.focus();
    };
  }, [dialogRef, initialFocusRef]);
}

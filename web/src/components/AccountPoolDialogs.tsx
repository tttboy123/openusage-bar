import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type RefObject,
} from "react";
import { X } from "@phosphor-icons/react";
import type {
  AccountPoolAccountViewModel,
  AccountPoolDefinition,
} from "../accountPools";
import type {
  AccountPoolStrategy,
  EditablePool,
} from "../accountPoolActions";
import type { Messages } from "../i18n";

const STRATEGIES: AccountPoolStrategy[] = [
  "fixed-first",
  "round-robin",
  "sticky",
  "quota-aware",
  "cost",
  "latency",
  "reliability",
];

interface EditorProps {
  mode: "create" | "edit";
  accounts: AccountPoolAccountViewModel[];
  pool: AccountPoolDefinition | null;
  pending: boolean;
  error: string | null;
  onCancel: () => void;
  onSubmit: (pool: EditablePool) => void;
  t: Messages;
}

interface MemberDraft {
  selected: boolean;
  priority: number;
  weight: number;
}

export function AccountPoolEditorDialog({
  mode,
  accounts,
  pool,
  pending,
  error,
  onCancel,
  onSubmit,
  t,
}: EditorProps) {
  const [poolId, setPoolId] = useState(pool?.poolId ?? "");
  const [strategy, setStrategy] = useState<AccountPoolStrategy>(
    pool?.strategy ?? "fixed-first",
  );
  const [members, setMembers] = useState<Record<string, MemberDraft>>(() =>
    Object.fromEntries(
      accounts.map((account, index) => {
        const configured = pool?.members.find(
          (member) => member.displayId === account.displayId,
        );
        return [
          account.displayId,
          {
            selected: configured !== undefined || (pool === null && index === 0),
            priority: configured?.priority ?? 100,
            weight: configured?.weight ?? 1,
          },
        ];
      }),
    ),
  );
  const [crossProviderFallback, setCrossProviderFallback] = useState(
    pool?.crossProviderFallback ?? false,
  );
  const [crossModelFallback, setCrossModelFallback] = useState(
    pool?.crossModelFallback ?? false,
  );
  const [crossRegionFallback, setCrossRegionFallback] = useState(
    pool?.crossRegionFallback ?? false,
  );
  const dialogRef = useRef<HTMLDivElement>(null);
  const firstFieldRef = useRef<HTMLInputElement>(null);
  useDialogKeyboard(dialogRef, firstFieldRef, pending, onCancel);

  const selectedMembers = useMemo(
    () =>
      accounts.flatMap((account) => {
        const member = members[account.displayId];
        return member?.selected
          ? [
              {
                displayId: account.displayId,
                priority: member.priority,
                weight: member.weight,
              },
            ]
          : [];
      }),
    [accounts, members],
  );
  const poolIdValid =
    /^[A-Za-z0-9._-]+$/u.test(poolId) && poolId.length <= 128;
  const membersValid =
    selectedMembers.length >= 1 &&
    selectedMembers.every(
      (member) =>
        Number.isSafeInteger(member.priority) &&
        member.priority >= 0 &&
        member.priority <= 10_000 &&
        Number.isSafeInteger(member.weight) &&
        member.weight >= 1 &&
        member.weight <= 100,
    );
  const valid = poolIdValid && membersValid;

  function updateMember(displayId: string, next: Partial<MemberDraft>) {
    setMembers((current) => ({
      ...current,
      [displayId]: { ...current[displayId], ...next },
    }));
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!valid || pending) {
      firstFieldRef.current?.focus();
      return;
    }
    onSubmit({
      poolId,
      strategy,
      members: selectedMembers,
      crossProviderFallback,
      crossModelFallback,
      crossRegionFallback,
    });
  }

  const titleId = "account-pool-editor-title";
  const descriptionId = "account-pool-editor-description";
  return (
    <div className="dialog-overlay" role="presentation" onMouseDown={pending ? undefined : onCancel}>
      <div
        ref={dialogRef}
        className="dialog account-pool-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={descriptionId}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="dialog-head">
          <div>
            <h3 id={titleId}>{mode === "create" ? t.createPool : t.editPool}</h3>
            <p id={descriptionId}>{t.createPoolUnverified}</p>
          </div>
          <button
            type="button"
            className="icon-btn"
            onClick={onCancel}
            disabled={pending}
            aria-label={t.close}
          >
            <X size={16} />
          </button>
        </div>
        <form className="account-pool-form" onSubmit={submit} noValidate>
          <label className="account-pool-field">
            <span>{t.poolId}</span>
            <input
              ref={firstFieldRef}
              className="text-input mono"
              value={poolId}
              onChange={(event) => setPoolId(event.target.value)}
              readOnly={mode === "edit"}
              disabled={pending}
              maxLength={128}
              aria-invalid={!poolIdValid}
              aria-describedby={!poolIdValid ? "account-pool-id-error" : undefined}
            />
          </label>
          {!poolIdValid ? (
            <p id="account-pool-id-error" className="account-pool-field-error">
              {t.poolIdInvalid}
            </p>
          ) : null}
          {pool ? (
            <p className="account-pool-revision">{t.poolRevision}: {pool.revision}</p>
          ) : null}
          <label className="account-pool-field">
            <span>{t.poolStrategy}</span>
            <select
              className="filter-select"
              value={strategy}
              onChange={(event) => setStrategy(event.target.value as AccountPoolStrategy)}
              disabled={pending}
            >
              {STRATEGIES.map((value) => (
                <option key={value} value={value}>{value}</option>
              ))}
            </select>
          </label>

          <fieldset className="account-pool-members-fieldset">
            <legend>{t.poolMembers}</legend>
            {accounts.map((account) => {
              const member = members[account.displayId];
              return (
                <div className="account-pool-member-row" key={account.displayId}>
                  <label className="account-pool-member-choice">
                    <input
                      type="checkbox"
                      checked={member?.selected ?? false}
                      onChange={(event) =>
                        updateMember(account.displayId, { selected: event.target.checked })
                      }
                      disabled={pending}
                    />
                    <span>
                      <strong>{account.alias ?? account.displayId}</strong>
                      <small>{account.providerId} · {account.displayId}</small>
                    </span>
                  </label>
                  <label>
                    <span>{t.priority}</span>
                    <input
                      type="number"
                      min={0}
                      max={10_000}
                      step={1}
                      value={member?.priority ?? 100}
                      disabled={pending || !member?.selected}
                      aria-invalid={member?.selected && (
                        !Number.isSafeInteger(member.priority) ||
                        member.priority < 0 ||
                        member.priority > 10_000
                      )}
                      onChange={(event) =>
                        updateMember(account.displayId, { priority: Number(event.target.value) })
                      }
                    />
                  </label>
                  <label>
                    <span>{t.weight}</span>
                    <input
                      type="number"
                      min={1}
                      max={100}
                      step={1}
                      value={member?.weight ?? 1}
                      disabled={pending || !member?.selected}
                      aria-invalid={member?.selected && (
                        !Number.isSafeInteger(member.weight) ||
                        member.weight < 1 ||
                        member.weight > 100
                      )}
                      onChange={(event) =>
                        updateMember(account.displayId, { weight: Number(event.target.value) })
                      }
                    />
                  </label>
                </div>
              );
            })}
            {!membersValid ? (
              <p className="account-pool-field-error">{t.poolMembersInvalid}</p>
            ) : null}
          </fieldset>

          <fieldset className="account-pool-fallback-fieldset">
            <legend>{t.poolFallbackConfirmations}</legend>
            <p>{t.poolFallbackWarning}</p>
            <label>
              <input
                type="checkbox"
                checked={crossProviderFallback}
                onChange={(event) => setCrossProviderFallback(event.target.checked)}
                disabled={pending}
              />
              {t.confirmCrossProviderFallback}
            </label>
            <label>
              <input
                type="checkbox"
                checked={crossModelFallback}
                onChange={(event) => setCrossModelFallback(event.target.checked)}
                disabled={pending}
              />
              {t.confirmCrossModelFallback}
            </label>
            <label>
              <input
                type="checkbox"
                checked={crossRegionFallback}
                onChange={(event) => setCrossRegionFallback(event.target.checked)}
                disabled={pending}
              />
              {t.confirmCrossRegionFallback}
            </label>
          </fieldset>

          {error ? <p className="account-pool-dialog-error" role="alert">{error}</p> : null}
          <div className="account-pool-dialog-actions">
            <button type="button" className="secondary-btn" onClick={onCancel} disabled={pending}>
              {t.cancel}
            </button>
            <button type="submit" className="primary-btn" disabled={!valid || pending}>
              {pending ? t.savingPool : mode === "create" ? t.createPool : t.savePool}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

interface RemoveProps {
  pool: AccountPoolDefinition;
  pending: boolean;
  error: string | null;
  onCancel: () => void;
  onConfirm: () => void;
  t: Messages;
}

export function AccountPoolRemoveDialog({
  pool,
  pending,
  error,
  onCancel,
  onConfirm,
  t,
}: RemoveProps) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  useDialogKeyboard(dialogRef, cancelRef, pending, onCancel);
  return (
    <div className="dialog-overlay" role="presentation" onMouseDown={pending ? undefined : onCancel}>
      <div
        ref={dialogRef}
        className="dialog account-pool-dialog account-pool-remove-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="remove-pool-title"
        aria-describedby="remove-pool-description"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="dialog-head">
          <h3 id="remove-pool-title">{t.removePool}</h3>
        </div>
        <p id="remove-pool-description">
          {t.removePoolDescription.replace("{pool}", pool.poolId)}
        </p>
        <p className="account-pool-revision">{t.poolRevision}: {pool.revision}</p>
        {error ? <p className="account-pool-dialog-error" role="alert">{error}</p> : null}
        <div className="account-pool-dialog-actions">
          <button ref={cancelRef} type="button" className="secondary-btn" onClick={onCancel} disabled={pending}>
            {t.cancel}
          </button>
          <button type="button" className="danger-btn" onClick={onConfirm} disabled={pending}>
            {pending ? t.removingPool : t.removePool}
          </button>
        </div>
      </div>
    </div>
  );
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
          'button:not(:disabled), input:not(:disabled), select:not(:disabled), [tabindex]:not([tabindex="-1"])',
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

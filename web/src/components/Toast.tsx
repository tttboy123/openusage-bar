import {
  createContext,
  useCallback,
  useContext,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { CheckCircle, WarningCircle, X } from "@phosphor-icons/react";
import { useLang } from "../i18n";

type ToastKind = "success" | "error" | "warning";
interface ToastItem {
  id: number;
  kind: ToastKind;
  message: string;
}

interface ToastApi {
  show: (kind: ToastKind, message: string) => void;
}

const ToastContext = createContext<ToastApi>({ show: () => {} });

export function useToast() {
  return useContext(ToastContext);
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const [leaving, setLeaving] = useState<Set<number>>(new Set());
  const nextId = useRef(1);
  const lang = useLang();
  const dismissLabel = lang === "zh" ? "关闭" : "Dismiss";

  const dismiss = useCallback((id: number) => {
    setLeaving((current) => new Set(current).add(id));
    window.setTimeout(() => {
      setLeaving((current) => {
        const next = new Set(current);
        next.delete(id);
        return next;
      });
      setItems((current) => current.filter((item) => item.id !== id));
    }, 160);
  }, []);

  const show = useCallback(
    (kind: ToastKind, message: string) => {
      const id = nextId.current++;
      setItems((current) => [...current, { id, kind, message }]);
      window.setTimeout(() => dismiss(id), 3350);
    },
    [dismiss],
  );

  return (
    <ToastContext.Provider value={{ show }}>
      {children}
      <div className="toast-stack" aria-live="polite">
        {items.map((item) => (
          <div
            key={item.id}
            className={`toast toast-${item.kind}${leaving.has(item.id) ? " leaving" : ""}`}
          >
            {item.kind === "success" ? (
              <CheckCircle size={16} />
            ) : (
              <WarningCircle size={16} />
            )}
            <span>{item.message}</span>
            <button
              type="button"
              className="toast-close"
              onClick={() => dismiss(item.id)}
              aria-label={dismissLabel}
            >
              <X size={16} />
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

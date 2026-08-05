import {
  createContext,
  useCallback,
  useContext,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { CheckCircle, WarningCircle, X } from "@phosphor-icons/react";

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
  const nextId = useRef(1);

  const dismiss = useCallback((id: number) => {
    setItems((current) => current.filter((item) => item.id !== id));
  }, []);

  const show = useCallback(
    (kind: ToastKind, message: string) => {
      const id = nextId.current++;
      setItems((current) => [...current, { id, kind, message }]);
      window.setTimeout(() => dismiss(id), 3500);
    },
    [dismiss],
  );

  return (
    <ToastContext.Provider value={{ show }}>
      {children}
      <div className="toast-stack" aria-live="polite">
        {items.map((item) => (
          <div key={item.id} className={`toast toast-${item.kind}`}>
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
              aria-label="Dismiss"
            >
              <X size={14} />
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

import { createContext, ReactNode, useCallback, useContext, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Notification, Typography, useDesignSystemTheme } from "@databricks/design-system";

type Toast = { id: number; title: string; description?: ReactNode; severity?: "info" | "success" | "warning" | "error";
  /** A link in the toast, as Databricks' "View run". */
  link?: { to: string; text: string } };
const Ctx = createContext<(t: Omit<Toast, "id">) => void>(() => {});

/** Toasts at the top right, as Databricks shows "Repair run started · View run". */
export function ToastProvider({ children }: { children: ReactNode }) {
  const { theme } = useDesignSystemTheme();
  const nav = useNavigate();
  const [toasts, setToasts] = useState<Toast[]>([]);
  const notify = useCallback((t: Omit<Toast, "id">) => setToasts((ts) => [...ts, { ...t, id: Date.now() + Math.random() }]), []);
  const close = (id: number) => setToasts((ts) => ts.filter((t) => t.id !== id));
  return (
    <Ctx.Provider value={notify}>
      <Notification.Provider duration={8000}>
        {children}
        {toasts.map((t) => (
          <Notification.Root key={t.id} componentId="toast" severity={t.severity ?? "success"}
                             onOpenChange={(open) => { if (!open) close(t.id); }}>
            <Notification.Title>{t.title}</Notification.Title>
            {(t.description || t.link) && (
              <Notification.Description>
                {t.description}
                {t.link && <>{t.description ? " " : ""}<Typography.Link componentId="toast.link"
                  onClick={() => { const to = t.link!.to; close(t.id); nav(to); }}>{t.link.text}</Typography.Link></>}
              </Notification.Description>
            )}
            <Notification.Close componentId="toast.close" />
          </Notification.Root>
        ))}
        <Notification.Viewport style={{ position: "fixed", top: theme.spacing.lg, right: theme.spacing.lg, bottom: "auto",
                                        left: "auto", zIndex: 2000 }} />
      </Notification.Provider>
    </Ctx.Provider>
  );
}

export const useToast = () => useContext(Ctx);

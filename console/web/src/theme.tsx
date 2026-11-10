import { createContext, useContext, useEffect, useState } from "react";

/** Databricks' appearance setting: light, dark, or follow the system. */
export type Appearance = "light" | "dark" | "system";

const KEY = "mlops-console.appearance";
const Ctx = createContext<{ appearance: Appearance; setAppearance: (a: Appearance) => void }>({
  appearance: "system", setAppearance: () => {},
});

function stored(): Appearance {
  try {
    const v = localStorage.getItem(KEY);
    return v === "light" || v === "dark" ? v : "system";
  } catch {
    return "system";
  }
}

const systemDark = () => window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? false;

/** The chosen appearance and whether it resolves to dark, kept across visits. */
export function useAppearanceState() {
  const [appearance, set] = useState<Appearance>(stored);
  const [sysDark, setSysDark] = useState(systemDark);
  useEffect(() => {
    const mq = window.matchMedia?.("(prefers-color-scheme: dark)");
    if (!mq) return;
    const on = () => setSysDark(mq.matches);
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);
  const setAppearance = (a: Appearance) => {
    set(a);
    try { localStorage.setItem(KEY, a); } catch { /* the choice lasts this visit only */ }
  };
  return { appearance, setAppearance, isDark: appearance === "dark" || (appearance === "system" && sysDark) };
}

export const AppearanceProvider = Ctx.Provider;
export const useAppearance = () => useContext(Ctx);

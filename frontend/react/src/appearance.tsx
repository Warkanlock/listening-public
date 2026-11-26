import { createContext, useContext, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { api, postJson } from "./api";

export const THEMES = [
  { id: "sea-glass", name: "Sea Glass", description: "Soft greens and a light background" },
  { id: "night", name: "Night", description: "Muted teal and a dark background" },
  { id: "paper", name: "Paper", description: "High-contrast grayscale" },
] as const;

type Theme = (typeof THEMES)[number]["id"];
const DEFAULT_THEME: Theme = "sea-glass";
const APPEARANCE_PATH = "/api/my/appearance";

function readTheme(result: unknown): Theme {
  if (typeof result === "object" && result !== null && "theme" in result) {
    return THEMES.find(theme => theme.id === result.theme)?.id ?? DEFAULT_THEME;
  }
  return DEFAULT_THEME;
}

type Appearance = {
  theme: Theme;
  pending: boolean;
  error: string;
  select(theme: Theme): Promise<void>;
};
const AppearanceContext = createContext<Appearance | null>(null);

export function useAppearance() {
  return useContext(AppearanceContext);
}

function applyTheme(theme: Theme) {
  const root = document.documentElement;
  if (theme === DEFAULT_THEME) delete root.dataset.roomTheme;
  else root.dataset.roomTheme = theme;
  const background = getComputedStyle(root).getPropertyValue("--bg").trim();
  if (background) document.querySelector('meta[name="theme-color"]')?.setAttribute("content", background);
}

export function AppearanceProvider({ account, children }: { account: string | null; children: ReactNode }) {
  const [theme, setTheme] = useState<Theme>(DEFAULT_THEME);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const read = useRef<AbortController | null>(null);
  const write = useRef<AbortController | null>(null);

  useLayoutEffect(() => applyTheme(theme), [theme]);
  useLayoutEffect(() => () => applyTheme(DEFAULT_THEME), []);

  useEffect(() => {
    if (!account) return;
    const refresh = () => {
      if (document.hidden || read.current || write.current) return;
      const controller = new AbortController();
      read.current = controller;
      void api<unknown>(APPEARANCE_PATH, { cache: "no-store", signal: controller.signal })
        .then(result => {
          if (!controller.signal.aborted) {
            setTheme(readTheme(result));
            setError("");
          }
        })
        .catch(() => {
          // Keep the last confirmed theme when the room is temporarily unreachable.
        })
        .finally(() => { if (read.current === controller) read.current = null; });
    };
    refresh();
    window.addEventListener("online", refresh);
    window.addEventListener("pageshow", refresh);
    window.addEventListener("focus", refresh);
    document.addEventListener("visibilitychange", refresh);
    return () => {
      read.current?.abort();
      read.current = null;
      write.current?.abort();
      write.current = null;
      window.removeEventListener("online", refresh);
      window.removeEventListener("pageshow", refresh);
      window.removeEventListener("focus", refresh);
      document.removeEventListener("visibilitychange", refresh);
    };
  }, [account]);

  async function select(next: Theme) {
    if (!account || write.current || next === theme) return;
    read.current?.abort();
    read.current = null;
    const controller = new AbortController();
    write.current = controller;
    setPending(true);
    setError("");
    try {
      const result = await postJson<unknown>(APPEARANCE_PATH, { theme: next }, controller.signal);
      if (!controller.signal.aborted) setTheme(readTheme(result));
    } catch {
      if (!controller.signal.aborted) setError("Couldn’t save the theme. Try again.");
    } finally {
      if (write.current === controller) {
        write.current = null;
        setPending(false);
      }
    }
  }

  return <AppearanceContext.Provider value={account ? { theme, pending, error, select } : null}>
    {children}
  </AppearanceContext.Provider>;
}

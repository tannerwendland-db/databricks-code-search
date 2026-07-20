// Light/dark theme: defaults to the OS preference (prefers-color-scheme), overridable via a
// toggle persisted to localStorage. Applies data-theme="light"|"dark" on <html>; CSS
// (src/index.css) keys off both prefers-color-scheme (for a first paint before hydration) and
// the explicit attribute (so the toggle can override it).

import { useEffect, useSyncExternalStore } from "react";

const STORAGE_KEY = "webui-theme";
export type Theme = "light" | "dark";

function systemTheme(): Theme {
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function readStoredTheme(): Theme | null {
  const stored = window.localStorage.getItem(STORAGE_KEY);
  return stored === "light" || stored === "dark" ? stored : null;
}

let current: Theme = readStoredTheme() ?? systemTheme();
const listeners = new Set<() => void>();

function applyToDocument(theme: Theme) {
  document.documentElement.setAttribute("data-theme", theme);
}
applyToDocument(current);

export function setTheme(theme: Theme): void {
  current = theme;
  window.localStorage.setItem(STORAGE_KEY, theme);
  applyToDocument(theme);
  listeners.forEach((l) => l());
}

export function toggleTheme(): void {
  setTheme(current === "dark" ? "light" : "dark");
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function useTheme(): Theme {
  useEffect(() => {
    if (readStoredTheme() !== null) return; // explicit user choice wins; don't chase the OS
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = () => setTheme(systemTheme());
    media.addEventListener("change", onChange);
    return () => media.removeEventListener("change", onChange);
  }, []);
  return useSyncExternalStore(subscribe, () => current);
}

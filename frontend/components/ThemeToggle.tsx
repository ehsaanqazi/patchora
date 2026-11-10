"use client";

import clsx from "clsx";
import { Moon, Sun } from "lucide-react";
import { useSyncExternalStore } from "react";

type Theme = "light" | "dark";
const STORAGE_KEY = "patchora-theme";

/**
 * Inline script for <head>: applies the saved theme (or the OS preference) before first paint so
 * the page never flashes the wrong palette. Keep it dependency-free and tiny.
 */
export const THEME_INIT_SCRIPT = `(function(){try{var t=localStorage.getItem("${STORAGE_KEY}");if(t!=="light"&&t!=="dark"){t=window.matchMedia("(prefers-color-scheme: light)").matches?"light":"dark";}document.documentElement.dataset.theme=t;}catch(e){}})();`;

function currentTheme(): Theme {
  const t = document.documentElement.dataset.theme;
  if (t === "light" || t === "dark") return t;
  return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

function savedTheme(): Theme | null {
  try {
    const t = localStorage.getItem(STORAGE_KEY);
    return t === "light" || t === "dark" ? t : null;
  } catch {
    return null;
  }
}

/** Subscribes to theme changes: the html data-theme attribute, plus OS changes while no choice is saved. */
function subscribe(notify: () => void): () => void {
  const observer = new MutationObserver(notify);
  observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  const mq = window.matchMedia("(prefers-color-scheme: light)");
  const onOsChange = () => {
    if (savedTheme() === null) document.documentElement.dataset.theme = mq.matches ? "light" : "dark";
  };
  mq.addEventListener("change", onOsChange);
  return () => {
    observer.disconnect();
    mq.removeEventListener("change", onOsChange);
  };
}

export function ThemeToggle({ className }: { className?: string }) {
  // Server snapshot is undefined: the theme is only known in the browser.
  const theme = useSyncExternalStore<Theme | undefined>(subscribe, currentTheme, () => undefined);

  const toggle = () => {
    const next: Theme = currentTheme() === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem(STORAGE_KEY, next); } catch { /* storage unavailable */ }
  };

  const label = theme === "light" ? "Switch to dark mode" : "Switch to light mode";
  return (
    <button type="button" onClick={toggle} aria-label={label} title={label}
      className={clsx("grid size-8 place-items-center rounded-md text-ink-muted transition-colors hover:bg-surface-3 hover:text-ink", className)}>
      {theme === undefined ? <span className="size-4" aria-hidden />
        : theme === "light" ? <Moon className="size-4" aria-hidden /> : <Sun className="size-4" aria-hidden />}
    </button>
  );
}

"use client";

import clsx from "clsx";
import {
  Bot,
  Bug,
  CheckSquare,
  FlaskConical,
  FolderGit2,
  GitPullRequest,
  LayoutDashboard,
  LogOut,
  Menu,
  Plug,
  Settings,
  ShieldCheck,
  X,
} from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { type ReactNode, useState } from "react";

import { useApp } from "@/components/AppProviders";
import { Badge } from "@/components/ui";
import { api } from "@/lib/api";

const NAV = [
  { href: "/", label: "Overview", icon: LayoutDashboard },
  { href: "/issues", label: "Sentry Issues", icon: Bug },
  { href: "/repositories", label: "Repositories", icon: FolderGit2 },
  { href: "/runs", label: "Agent Runs", icon: Bot },
  { href: "/pull-requests", label: "Pull Requests", icon: GitPullRequest },
  { href: "/approvals", label: "Approvals", icon: CheckSquare },
  { href: "/evaluations", label: "Evaluations", icon: FlaskConical },
  { href: "/integrations", label: "Integrations", icon: Plug },
  { href: "/settings", label: "Settings", icon: Settings },
];

function NavLinks({ onNavigate }: { onNavigate?: () => void }) {
  const pathname = usePathname();
  return (
    <ul className="space-y-0.5">
      {NAV.map(({ href, label, icon: Icon }) => {
        const active = href === "/" ? pathname === "/" : pathname.startsWith(href);
        return (
          <li key={href}>
            <Link href={href} onClick={onNavigate} aria-current={active ? "page" : undefined}
              className={clsx("flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition-colors",
                active ? "bg-surface-3 text-ink" : "text-ink-muted hover:bg-surface-2 hover:text-ink")}>
              <Icon className={clsx("size-4", active && "text-accent-strong")} aria-hidden />
              {label}
            </Link>
          </li>
        );
      })}
    </ul>
  );
}

function Brand() {
  return (
    <Link href="/" className="flex items-center gap-2.5 px-3">
      <span className="grid size-8 place-items-center rounded-lg bg-accent-soft text-accent-strong">
        <ShieldCheck className="size-4.5" aria-hidden />
      </span>
      <span>
        <span className="block text-sm font-semibold text-ink">Patchora</span>
        <span className="block text-[11px] text-ink-faint">Investigate · Repair · Review</span>
      </span>
    </Link>
  );
}

function StatusFooter() {
  const { me, diagnostics } = useApp();
  const router = useRouter();
  return (
    <div className="space-y-3 border-t border-line px-3 pt-4 text-xs">
      {diagnostics && (
        <div className="space-y-1.5">
          <div className="flex items-center justify-between">
            <span className="text-ink-faint">Sandbox</span>
            <Badge tone={diagnostics.sandbox.code_execution_enabled ? "ok" : "warn"}
              title={diagnostics.sandbox.detail}>
              {diagnostics.sandbox.code_execution_enabled ? diagnostics.sandbox.backend : "execution off"}
            </Badge>
          </div>
          <div className="flex items-center justify-between">
            <span className="text-ink-faint">Mode</span>
            <span className="text-ink-muted">{diagnostics.autonomy_mode.replace(/_/g, " ")}</span>
          </div>
        </div>
      )}
      {me && (
        <div className="flex items-center justify-between gap-2">
          <div className="min-w-0">
            <p className="truncate text-ink">{me.display_name}</p>
            <p className="text-ink-faint">{me.role}{me.auth_mode === "local" ? " · local dev mode" : ""}</p>
          </div>
          {me.auth_mode === "token" && (
            <button type="button" aria-label="Sign out" className="rounded-md p-1.5 text-ink-faint hover:text-ink"
              onClick={() => api.logout().finally(() => router.replace("/login"))}>
              <LogOut className="size-4" />
            </button>
          )}
        </div>
      )}
    </div>
  );
}

export function Shell({ children }: { children: ReactNode }) {
  const [open, setOpen] = useState(false);
  const pathname = usePathname();
  if (pathname === "/login") return <>{children}</>;
  return (
    <div className="min-h-dvh lg:grid lg:grid-cols-[248px_1fr]">
      <a href="#main" className="sr-only focus:not-sr-only focus:absolute focus:left-3 focus:top-3 focus:z-50 focus:rounded-md focus:bg-surface-3 focus:px-3 focus:py-2">
        Skip to content
      </a>
      <aside className="sticky top-0 hidden h-dvh flex-col justify-between border-r border-line bg-surface py-5 lg:flex">
        <div className="space-y-6">
          <Brand />
          <nav aria-label="Main" className="px-2"><NavLinks /></nav>
        </div>
        <StatusFooter />
      </aside>

      <header className="sticky top-0 z-30 flex items-center justify-between border-b border-line bg-surface/95 px-4 py-3 backdrop-blur lg:hidden">
        <Brand />
        <button type="button" aria-label={open ? "Close navigation" : "Open navigation"} aria-expanded={open}
          className="rounded-md p-2 text-ink-muted hover:bg-surface-3" onClick={() => setOpen(!open)}>
          {open ? <X className="size-5" /> : <Menu className="size-5" />}
        </button>
      </header>
      {open && (
        <div className="fixed inset-x-0 top-[57px] z-20 border-b border-line bg-surface p-3 lg:hidden">
          <nav aria-label="Main"><NavLinks onNavigate={() => setOpen(false)} /></nav>
          <div className="mt-3"><StatusFooter /></div>
        </div>
      )}

      <main id="main" className="min-w-0 px-4 py-6 sm:px-6 lg:px-10 lg:py-8">
        <div className="mx-auto max-w-7xl">{children}</div>
      </main>
    </div>
  );
}

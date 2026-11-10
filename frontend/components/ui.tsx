"use client";

import clsx from "clsx";
import { AlertTriangle, ChevronLeft, ChevronRight, Inbox, Loader2, RefreshCw } from "lucide-react";
import Link from "next/link";
import {
  type ButtonHTMLAttributes,
  type InputHTMLAttributes,
  type ReactNode,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
  useEffect,
  useId,
  useRef,
} from "react";

import { ApiError } from "@/lib/api";

// --- Buttons ---------------------------------------------------------------------------------

type Variant = "primary" | "secondary" | "ghost" | "danger";
const VARIANTS: Record<Variant, string> = {
  primary: "bg-accent text-white hover:bg-accent-strong disabled:bg-accent/40",
  secondary: "bg-surface-3 text-ink border border-line-strong hover:border-ink-faint disabled:opacity-50",
  ghost: "text-ink-muted hover:text-ink hover:bg-surface-3 disabled:opacity-50",
  danger: "bg-bad/90 text-white hover:bg-bad disabled:bg-bad/40",
};

export function Button({
  variant = "secondary", size = "md", loading, icon, children, className, disabled, ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: Variant; size?: "sm" | "md"; loading?: boolean; icon?: ReactNode;
}) {
  return (
    <button
      type="button"
      {...props}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      className={clsx(
        "inline-flex items-center justify-center gap-2 rounded-lg font-medium transition-colors disabled:cursor-not-allowed",
        size === "sm" ? "h-8 px-3 text-xs" : "h-9 px-4 text-sm",
        VARIANTS[variant],
        className,
      )}
    >
      {loading ? <Loader2 className="size-4 animate-spin" aria-hidden /> : icon}
      {children}
    </button>
  );
}

export function LinkButton({ href, children, variant = "secondary", className }: {
  href: string; children: ReactNode; variant?: Variant; className?: string;
}) {
  return (
    <Link href={href} className={clsx(
      "inline-flex h-9 items-center justify-center gap-2 rounded-lg px-4 text-sm font-medium transition-colors",
      VARIANTS[variant], className)}>
      {children}
    </Link>
  );
}

// --- Layout ----------------------------------------------------------------------------------

export function PageHeader({ title, description, actions, badge }: {
  title: string; description?: ReactNode; actions?: ReactNode; badge?: ReactNode;
}) {
  return (
    <div className="mb-6 flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="text-xl font-semibold tracking-tight text-ink">{title}</h1>
          {badge}
        </div>
        {description && <p className="mt-1 max-w-3xl text-sm text-ink-muted">{description}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}

export function Card({ title, description, actions, children, className, padded = true }: {
  title?: ReactNode; description?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string;
  padded?: boolean;
}) {
  return (
    <section className={clsx("rounded-xl border border-line bg-surface", className)}>
      {(title || actions) && (
        <header className="flex flex-wrap items-start justify-between gap-3 border-b border-line px-5 py-3.5">
          <div className="min-w-0">
            {title && <h2 className="text-sm font-semibold text-ink">{title}</h2>}
            {description && <p className="mt-0.5 text-xs text-ink-muted">{description}</p>}
          </div>
          {actions && <div className="flex items-center gap-2">{actions}</div>}
        </header>
      )}
      <div className={clsx(padded && "p-5")}>{children}</div>
    </section>
  );
}

export function StatCard({ label, value, hint, tone = "neutral", href }: {
  label: string; value: ReactNode; hint?: string; tone?: Tone; href?: string;
}) {
  const body = (
    <div className={clsx("h-full rounded-xl border border-line bg-surface p-4 transition-colors",
      href && "hover:border-line-strong")}>
      <p className="text-xs font-medium uppercase tracking-wide text-ink-faint">{label}</p>
      <p className={clsx("mt-2 text-2xl font-semibold tabular-nums", TONE_TEXT[tone])}>{value}</p>
      {hint && <p className="mt-1 text-xs text-ink-muted">{hint}</p>}
    </div>
  );
  return href ? <Link href={href} className="block h-full rounded-xl">{body}</Link> : body;
}

export function KeyValue({ items }: { items: [string, ReactNode][] }) {
  return (
    <dl className="grid grid-cols-1 gap-x-6 gap-y-3 text-sm sm:grid-cols-2">
      {items.map(([key, value]) => (
        <div key={key} className="min-w-0">
          <dt className="text-xs text-ink-faint">{key}</dt>
          <dd className="mt-0.5 break-words text-ink">{value}</dd>
        </div>
      ))}
    </dl>
  );
}

// --- Badges ----------------------------------------------------------------------------------

export type Tone = "neutral" | "info" | "ok" | "warn" | "bad" | "accent";
const TONE_BADGE: Record<Tone, string> = {
  neutral: "bg-surface-3 text-ink-muted border-line-strong",
  info: "bg-info-soft text-info border-info/30",
  ok: "bg-ok-soft text-ok border-ok/30",
  warn: "bg-warn-soft text-warn border-warn/30",
  bad: "bg-bad-soft text-bad border-bad/30",
  accent: "bg-accent-soft text-accent-strong border-accent/30",
};
const TONE_TEXT: Record<Tone, string> = {
  neutral: "text-ink", info: "text-info", ok: "text-ok", warn: "text-warn", bad: "text-bad", accent: "text-accent-strong",
};

export function Badge({ tone = "neutral", children, title }: { tone?: Tone; children: ReactNode; title?: string }) {
  return (
    <span title={title} className={clsx(
      "inline-flex items-center gap-1 whitespace-nowrap rounded-md border px-1.5 py-0.5 text-[11px] font-medium",
      TONE_BADGE[tone])}>
      {children}
    </span>
  );
}

export function DemoBadge() {
  return <Badge tone="accent" title="Synthetic demo data - not real production data">Demo</Badge>;
}

export function SimulatedBadge({ what }: { what: string }) {
  return <Badge tone="warn" title={`${what} was simulated, not produced by a real system`}>Simulated {what}</Badge>;
}

// --- Feedback states -------------------------------------------------------------------------

export function Skeleton({ className }: { className?: string }) {
  return <div className={clsx("animate-pulse rounded-md bg-surface-3", className)} aria-hidden />;
}

export function LoadingRows({ rows = 5 }: { rows?: number }) {
  return (
    <div className="space-y-2 p-4" role="status" aria-label="Loading">
      {Array.from({ length: rows }, (_, i) => <Skeleton key={i} className="h-9 w-full" />)}
    </div>
  );
}

export function EmptyState({ title, description, action, icon }: {
  title: string; description?: ReactNode; action?: ReactNode; icon?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center px-6 py-12 text-center">
      <div className="mb-3 rounded-full bg-surface-3 p-3 text-ink-faint">{icon ?? <Inbox className="size-5" />}</div>
      <p className="text-sm font-medium text-ink">{title}</p>
      {description && <p className="mt-1 max-w-md text-sm text-ink-muted">{description}</p>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}

export function ErrorState({ error, onRetry }: { error: Error; onRetry?: () => void }) {
  const notConfigured = error instanceof ApiError && error.code === "not_configured";
  return (
    <div role="alert" className="flex flex-col items-start gap-3 rounded-lg border border-bad/30 bg-bad-soft p-4 text-sm">
      <div className="flex items-start gap-2 text-bad">
        <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden />
        <div>
          <p className="font-medium">{notConfigured ? "Integration not configured" : "Something went wrong"}</p>
          <p className="mt-0.5 text-ink-muted">{error.message}</p>
          {notConfigured && (
            <Link href="/integrations" className="mt-1 inline-block text-accent-strong underline">
              Open integrations
            </Link>
          )}
        </div>
      </div>
      {onRetry && <Button size="sm" icon={<RefreshCw className="size-3.5" />} onClick={onRetry}>Retry</Button>}
    </div>
  );
}

export function InlineAlert({ tone = "info", children }: { tone?: Tone; children: ReactNode }) {
  return (
    <div role={tone === "bad" ? "alert" : "status"} className={clsx("rounded-lg border px-3 py-2 text-sm",
      TONE_BADGE[tone])}>
      {children}
    </div>
  );
}

// --- Tables & pagination ---------------------------------------------------------------------

export function Table({ children, caption }: { children: ReactNode; caption: string }) {
  return (
    <div className="scrollbar-thin overflow-x-auto">
      <table className="w-full min-w-[640px] border-collapse text-left text-sm">
        <caption className="sr-only">{caption}</caption>
        {children}
      </table>
    </div>
  );
}

export function Th({ children, className }: { children?: ReactNode; className?: string }) {
  return <th scope="col" className={clsx("border-b border-line px-4 py-2.5 text-xs font-medium text-ink-faint",
    className)}>{children}</th>;
}

export function Td({ children, className }: { children?: ReactNode; className?: string }) {
  return <td className={clsx("border-b border-line/60 px-4 py-3 align-top", className)}>{children}</td>;
}

export function Pagination({ total, limit, offset, onChange }: {
  total: number; limit: number; offset: number; onChange: (offset: number) => void;
}) {
  if (total <= limit) return null;
  const page = Math.floor(offset / limit) + 1;
  const pages = Math.ceil(total / limit);
  return (
    <nav aria-label="Pagination" className="flex items-center justify-between border-t border-line px-4 py-3 text-xs text-ink-muted">
      <span>{offset + 1}–{Math.min(offset + limit, total)} of {total}</span>
      <div className="flex items-center gap-1">
        <Button size="sm" variant="ghost" aria-label="Previous page" disabled={page <= 1}
          onClick={() => onChange(Math.max(0, offset - limit))} icon={<ChevronLeft className="size-4" />} />
        <span aria-current="page">Page {page} / {pages}</span>
        <Button size="sm" variant="ghost" aria-label="Next page" disabled={page >= pages}
          onClick={() => onChange(offset + limit)} icon={<ChevronRight className="size-4" />} />
      </div>
    </nav>
  );
}

// --- Forms -----------------------------------------------------------------------------------

export function Field({ label, hint, children, error }: {
  label: string; hint?: ReactNode; children: (id: string) => ReactNode; error?: string;
}) {
  const id = useId();
  return (
    <div className="space-y-1.5">
      <label htmlFor={id} className="block text-xs font-medium text-ink-muted">{label}</label>
      {children(id)}
      {hint && !error && <p className="text-xs text-ink-faint">{hint}</p>}
      {error && <p className="text-xs text-bad">{error}</p>}
    </div>
  );
}

/** Controls fill their container unless the caller sets an explicit width. */
const width = (className?: string) => (className && /(^|\s)w-/.test(className) ? "" : "w-full");

const CONTROL = "rounded-lg border border-line-strong bg-surface-2 px-3 text-sm text-ink placeholder:text-ink-faint " +
  "focus:border-accent focus:outline-none disabled:opacity-60";

export function Input(props: InputHTMLAttributes<HTMLInputElement>) {
  return <input {...props} className={clsx(CONTROL, width(props.className), "h-9", props.className)} />;
}

export function Select(props: SelectHTMLAttributes<HTMLSelectElement>) {
  return <select {...props} className={clsx(CONTROL, width(props.className), "h-9", props.className)} />;
}

export function Textarea(props: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea {...props} className={clsx(CONTROL, width(props.className), "py-2 font-mono text-xs", props.className)} />;
}

export function Toggle({ checked, onChange, label, description, disabled }: {
  checked: boolean; onChange: (value: boolean) => void; label: string; description?: string; disabled?: boolean;
}) {
  const id = useId();
  return (
    <div className="flex items-start justify-between gap-4">
      <div>
        <label htmlFor={id} className="text-sm text-ink">{label}</label>
        {description && <p className="text-xs text-ink-faint">{description}</p>}
      </div>
      <button id={id} type="button" role="switch" aria-checked={checked} disabled={disabled}
        onClick={() => onChange(!checked)}
        className={clsx("relative h-5 w-9 shrink-0 rounded-full transition-colors disabled:opacity-50",
          checked ? "bg-accent" : "bg-surface-3 border border-line-strong")}>
        <span className={clsx("absolute top-0.5 size-4 rounded-full bg-white transition-transform",
          checked ? "translate-x-4" : "translate-x-0.5")} />
      </button>
    </div>
  );
}

// --- Dialog ----------------------------------------------------------------------------------

/** Accessible modal built on the native <dialog> element (focus trap and Esc handling). */
export function Dialog({ open, onClose, title, children, footer }: {
  open: boolean; onClose: () => void; title: string; children: ReactNode; footer?: ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [open]);
  return (
    <dialog ref={ref} aria-labelledby={titleId} onClose={onClose} onCancel={onClose}
      className="m-auto w-[min(92vw,520px)] rounded-xl border border-line-strong bg-surface p-0 text-ink shadow-2xl">
      <div className="border-b border-line px-5 py-4">
        <h2 id={titleId} className="text-base font-semibold">{title}</h2>
      </div>
      <div className="space-y-3 px-5 py-4 text-sm text-ink-muted">{children}</div>
      {footer && <div className="flex justify-end gap-2 border-t border-line px-5 py-3">{footer}</div>}
    </dialog>
  );
}

export function ConfirmDialog({ open, title, children, confirmLabel, tone = "primary", onConfirm, onCancel, pending }: {
  open: boolean; title: string; children: ReactNode; confirmLabel: string; tone?: "primary" | "danger";
  onConfirm: () => void; onCancel: () => void; pending?: boolean;
}) {
  return (
    <Dialog open={open} onClose={onCancel} title={title} footer={
      <>
        <Button variant="ghost" onClick={onCancel} disabled={pending}>Cancel</Button>
        <Button variant={tone} onClick={onConfirm} loading={pending}>{confirmLabel}</Button>
      </>
    }>
      {children}
    </Dialog>
  );
}

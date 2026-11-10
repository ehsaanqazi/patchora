import { Badge, type Tone } from "@/components/ui";
import { humanize } from "@/lib/format";

const RUN_STATE_TONE: Record<string, Tone> = {
  COMPLETED: "ok",
  FAILED: "bad",
  CANCELLED: "neutral",
  NEEDS_HUMAN_REVIEW: "warn",
  WAITING_FOR_APPROVAL: "accent",
};

export function RunStatusBadge({ status }: { status: string }) {
  const tone = RUN_STATE_TONE[status] ?? "info";
  const active = !(status in RUN_STATE_TONE);
  return (
    <Badge tone={tone}>
      {active && <span className="size-1.5 animate-pulse rounded-full bg-current" aria-hidden />}
      {humanize(status)}
    </Badge>
  );
}

const OUTCOME_TONE: Record<string, Tone> = {
  pull_request_created: "ok",
  patch_validated: "ok",
  awaiting_approval: "accent",
  analysis_only: "info",
  abstained_insufficient_evidence: "warn",
  abstained_external_cause: "warn",
  no_repository: "warn",
  policy_blocked: "bad",
  tests_failed: "bad",
  patch_invalid: "bad",
  rejected: "neutral",
  duplicate: "neutral",
  cancelled: "neutral",
  error: "bad",
};

export function OutcomeBadge({ outcome }: { outcome: string | null | undefined }) {
  if (!outcome) return <span className="text-xs text-ink-faint">In progress</span>;
  return <Badge tone={OUTCOME_TONE[outcome] ?? "neutral"}>{humanize(outcome)}</Badge>;
}

const VALIDATION_TONE: Record<string, Tone> = {
  passed: "ok",
  failed: "bad",
  timed_out: "bad",
  setup_failed: "bad",
  security_blocked: "bad",
  not_available: "warn",
  not_run: "neutral",
  pending: "neutral",
};

export function ValidationBadge({ status, simulated }: { status: string | null | undefined; simulated?: boolean }) {
  if (!status) return <span className="text-xs text-ink-faint">—</span>;
  return (
    <Badge tone={VALIDATION_TONE[status] ?? "neutral"} title={simulated ? "Simulated result (demo mode)" : undefined}>
      {humanize(status)}{simulated ? " · simulated" : ""}
    </Badge>
  );
}

export function PolicyBadge({ status }: { status: string | null | undefined }) {
  const tone: Tone = status === "passed" ? "ok" : status === "failed" ? "bad" : "neutral";
  return <Badge tone={tone}>Policy {status ?? "pending"}</Badge>;
}

export function LevelBadge({ level }: { level: string | null | undefined }) {
  const tone: Tone = level === "fatal" || level === "error" ? "bad" : level === "warning" ? "warn" : "info";
  return <Badge tone={tone}>{level ?? "unknown"}</Badge>;
}

export function RiskBadge({ risk }: { risk: string | null | undefined }) {
  const tone: Tone = risk === "low" ? "ok" : risk === "medium" ? "warn" : risk === "high" ? "bad" : "neutral";
  return <Badge tone={tone}>Risk {risk ?? "unknown"}</Badge>;
}

export function PrStatusBadge({ status, draft }: { status: string; draft?: boolean }) {
  const tone: Tone = status === "merged" ? "accent" : status === "open" ? "ok" : "neutral";
  return <Badge tone={tone}>{draft && status === "open" ? "Draft" : humanize(status)}</Badge>;
}

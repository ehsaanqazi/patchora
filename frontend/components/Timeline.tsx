"use client";

import clsx from "clsx";
import { CheckCircle2, ChevronDown, CircleSlash, Loader2, RotateCw, XCircle } from "lucide-react";
import { useState } from "react";

import { Badge } from "@/components/ui";
import type { StepOut } from "@/lib/api";
import { formatDuration, humanize } from "@/lib/format";

interface LlmCall { purpose: string; model: string; prompt_tokens: number; completion_tokens: number;
  duration_ms: number; attempts: number; simulated: boolean }

function StepIcon({ status }: { status: string }) {
  if (status === "running") return <Loader2 className="size-4 animate-spin text-info" aria-label="Running" />;
  if (status === "succeeded") return <CheckCircle2 className="size-4 text-ok" aria-label="Succeeded" />;
  if (status === "failed") return <XCircle className="size-4 text-bad" aria-label="Failed" />;
  return <CircleSlash className="size-4 text-ink-faint" aria-label="Skipped" />;
}

function Step({ step }: { step: StepOut }) {
  const [open, setOpen] = useState(false);
  const meta = step.metadata as { llm_calls?: LlmCall[]; policy_findings?: unknown[]; files_inspected?: string[] };
  const calls = meta.llm_calls ?? [];
  const hasDetails = calls.length > 0 || !!meta.files_inspected?.length || !!meta.policy_findings?.length;
  return (
    <li className="relative pb-5 pl-8 last:pb-0">
      <span className="absolute left-[7px] top-6 h-[calc(100%-1rem)] w-px bg-line" aria-hidden />
      <span className="absolute left-0 top-0.5"><StepIcon status={step.status} /></span>
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-medium text-ink">{humanize(step.step_type)}</span>
        <span className="font-mono text-[11px] text-ink-faint">{step.state}</span>
        {step.retry_count > 0 && <Badge tone="warn"><RotateCw className="size-3" aria-hidden />retry {step.retry_count}</Badge>}
        <span className="ml-auto text-xs tabular-nums text-ink-faint">{formatDuration(step.duration_ms)}</span>
      </div>
      {step.output_summary && <p className="mt-1 text-sm text-ink-muted">{step.output_summary}</p>}
      {step.error_message && <p className="mt-1 text-sm text-bad">{step.error_message}</p>}
      {hasDetails && (
        <button type="button" onClick={() => setOpen(!open)} aria-expanded={open}
          className="mt-1.5 inline-flex items-center gap-1 text-xs text-accent-strong hover:underline">
          <ChevronDown className={clsx("size-3 transition-transform", open && "rotate-180")} aria-hidden />Details
        </button>
      )}
      {open && (
        <div className="mt-2 space-y-2 rounded-lg border border-line bg-surface-2 p-3 text-xs">
          {calls.map((call, i) => (
            <div key={i} className="flex flex-wrap gap-x-4 gap-y-1 text-ink-muted">
              <span className="text-ink">Model call: {call.purpose}</span>
              <span>{call.model}{call.simulated ? " (simulated)" : ""}</span>
              <span>{call.simulated ? "no tokens used" : `${call.prompt_tokens} in / ${call.completion_tokens} out tokens`}</span>
              <span>{formatDuration(call.duration_ms)}</span>
              {call.attempts > 1 && <span className="text-warn">{call.attempts} attempts (output re-validated)</span>}
            </div>
          ))}
          {meta.files_inspected?.length ? (
            <p className="text-ink-muted">Files inspected: <span className="font-mono">{meta.files_inspected.join(", ")}</span></p>
          ) : null}
          {meta.policy_findings?.length ? (
            <pre className="whitespace-pre-wrap text-ink-muted">{JSON.stringify(meta.policy_findings, null, 1)}</pre>
          ) : null}
        </div>
      )}
    </li>
  );
}

export function Timeline({ steps }: { steps: StepOut[] }) {
  if (!steps.length) return <p className="text-sm text-ink-muted">Waiting for a worker to pick up the run…</p>;
  return <ol aria-label="Agent steps">{steps.map((step) => <Step key={step.id} step={step} />)}</ol>;
}

const PHASES = [
  { label: "Intake", states: ["RECEIVED", "VALIDATING", "DEDUPLICATING"] },
  { label: "Sentry", states: ["FETCHING_SENTRY_DATA", "MAPPING_REPOSITORY"] },
  { label: "Investigate", states: ["FETCHING_SOURCE", "INVESTIGATING", "DIAGNOSIS_READY"] },
  { label: "Patch", states: ["PLANNING_PATCH", "GENERATING_PATCH", "REVIEWING_PATCH"] },
  { label: "Validate", states: ["APPLYING_PATCH", "RUNNING_TESTS", "EVALUATING_RESULTS"] },
  { label: "Review", states: ["WAITING_FOR_APPROVAL", "READY_TO_PUBLISH"] },
  { label: "Publish", states: ["PUBLISHING_BRANCH", "CREATING_PULL_REQUEST"] },
];

/** Compact progress indicator over the workflow's phases, derived from the recorded steps. */
export function PhaseBar({ steps, status }: { steps: StepOut[]; status: string }) {
  const reached = new Set(steps.map((s) => s.state));
  reached.add(status);
  const failedState = steps.findLast?.((s) => s.status === "failed")?.state;
  return (
    <ol className="grid grid-cols-7 gap-1" aria-label="Workflow progress">
      {PHASES.map((phase) => {
        const current = phase.states.includes(status);
        const done = !current && phase.states.some((s) => reached.has(s));
        const failed = failedState !== undefined && phase.states.includes(failedState);
        return (
          <li key={phase.label} aria-current={current ? "step" : undefined}>
            <div className={clsx("h-1.5 rounded-full",
              failed ? "bg-bad" : current ? "animate-pulse bg-info" : done ? "bg-ok" : "bg-surface-3")} />
            <p className={clsx("mt-1.5 truncate text-[11px]", current ? "text-ink" : "text-ink-faint")}>{phase.label}</p>
          </li>
        );
      })}
    </ol>
  );
}

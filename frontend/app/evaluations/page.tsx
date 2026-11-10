"use client";

import { FlaskConical } from "lucide-react";
import { useState } from "react";

import { useApp } from "@/components/AppProviders";
import {
  Badge, Button, Card, EmptyState, ErrorState, InlineAlert, LoadingRows, PageHeader, Table, Td, Th,
} from "@/components/ui";
import { useAction, useApi } from "@/hooks/useApi";
import { api, type EvaluationSummary } from "@/lib/api";
import { formatDate, formatDuration, humanize } from "@/lib/format";

const HEADLINE = [
  ["outcome_accuracy", "Outcome accuracy", "ratio"],
  ["correct_abstention_rate", "Correct abstention", "ratio"],
  ["pr_creation_success_rate", "PR creation success", "ratio"],
  ["patch_application_success_rate", "Patch application", "ratio"],
  ["relevant_file_retrieval_precision", "Retrieval precision", "ratio"],
  ["test_pass_rate", "Test pass rate", "ratio"],
  ["regressions_detected", "Regressions caught", "count"],
  ["runtime_ms_median", "Median runtime", "ms"],
] as const;

function show(value: unknown, kind: string): string {
  if (value === null || value === undefined) return "n/a";
  if (kind === "ratio") return `${Math.round(Number(value) * 100)}%`;
  if (kind === "ms") return formatDuration(Number(value));
  return String(value);
}

export default function EvaluationsPage() {
  const list = useApi(() => api.evaluations(), [], 15_000);
  const { isAdmin } = useApp();
  const [selected, setSelected] = useState<number>();
  const current = selected ?? list.data?.items[0]?.id;
  const detail = useApi(() => (current ? api.evaluation(current) : Promise.resolve(undefined)), [current]);
  const [started, setStarted] = useState(false);
  const start = useAction(async () => { await api.startEvaluation(); setStarted(true); });

  return (
    <>
      <PageHeader title="Evaluations"
        description="Measured results on the labeled synthetic dataset. Scripted runs measure the pipeline; Groq runs measure the model."
        actions={isAdmin && <Button icon={<FlaskConical className="size-4" />} loading={start.pending} onClick={() => start.run()}>Run scripted evaluation</Button>} />
      {start.error && <div className="mb-4"><ErrorState error={start.error} /></div>}
      {started && <div className="mb-4"><InlineAlert tone="info">Evaluation started in the background. Results appear here when it finishes (about a minute).</InlineAlert></div>}

      {list.loading && !list.data ? <LoadingRows /> : list.error ? <ErrorState error={list.error} onRetry={list.reload} />
        : !list.data?.items.length ? (
          <Card><EmptyState title="No evaluations yet"
            description={<>Run <code className="font-mono">make eval</code> (or the button above) to measure the pipeline on the synthetic dataset.</>} /></Card>
        ) : (
          <div className="grid gap-6 xl:grid-cols-4">
            <Card title="History" padded={false} className="xl:col-span-1">
              <ul className="divide-y divide-line/60">
                {list.data.items.map((e) => (
                  <li key={e.id}>
                    <button type="button" onClick={() => setSelected(e.id)} aria-current={e.id === current}
                      className={`w-full px-4 py-3 text-left text-xs hover:bg-surface-2 ${e.id === current ? "bg-surface-2" : ""}`}>
                      <p className="text-ink">{formatDate(e.started_at)}</p>
                      <p className="mt-0.5 text-ink-faint">{e.llm_mode} · {e.model_name} · dataset {e.dataset_version}</p>
                    </button>
                  </li>
                ))}
              </ul>
            </Card>
            <div className="space-y-6 xl:col-span-3">
              {detail.data ? <EvaluationDetail evaluation={detail.data} /> : <LoadingRows />}
            </div>
          </div>
        )}
    </>
  );
}

function EvaluationDetail({ evaluation }: { evaluation: EvaluationSummary }) {
  const m = evaluation.metrics;
  const results = (evaluation.results ?? []) as Record<string, unknown>[];
  return (
    <>
      <InlineAlert tone={evaluation.llm_mode === "scripted" ? "info" : "accent"}>
        {evaluation.llm_mode === "scripted"
          ? "Scripted mode replays fixed model responses: these numbers measure the pipeline (evidence, gates, sandboxed validation, publication, idempotency), not model quality."
          : `Model mode: responses came from ${evaluation.model_name}.`}
        {" "}Config {evaluation.config_version}.
      </InlineAlert>
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        {HEADLINE.map(([key, label, kind]) => (
          <div key={key} className="rounded-xl border border-line bg-surface p-4">
            <p className="text-xs text-ink-faint">{label}</p>
            <p className="mt-1 text-xl font-semibold tabular-nums">{show(m[key], kind)}</p>
          </div>
        ))}
      </div>
      <Card title="Per-case results" padded={false}>
        <Table caption="Evaluation cases">
          <thead><tr><Th>Case</Th><Th>Category</Th><Th>Outcome</Th><Th>Expected</Th><Th>Validation</Th><Th>Repairs</Th><Th>Runtime</Th></tr></thead>
          <tbody>
            {results.map((r) => (
              <tr key={String(r.key)}>
                <Td><p className="font-mono text-xs text-ink">{String(r.key)}</p><p className="mt-0.5 max-w-xs text-xs text-ink-faint">{String(r.description ?? "")}</p></Td>
                <Td className="text-xs">{String(r.category)}</Td>
                <Td>{r.skipped ? <Badge>skipped</Badge> : <Badge tone={r.outcome_correct ? "ok" : "bad"}>{humanize(String(r.outcome))}</Badge>}</Td>
                <Td className="text-xs text-ink-muted">{((r.expected_outcomes as string[]) ?? []).map(humanize).join(", ")}</Td>
                <Td className="text-xs">{r.final_validation ? humanize(String(r.final_validation)) : "—"}</Td>
                <Td className="text-xs tabular-nums">{String(r.repair_attempts ?? "—")}</Td>
                <Td className="text-xs tabular-nums">{formatDuration(r.duration_ms as number | null)}</Td>
              </tr>
            ))}
          </tbody>
        </Table>
      </Card>
      <Card title="All metrics">
        <dl className="grid grid-cols-1 gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
          {Object.entries(m).map(([k, v]) => (
            <div key={k} className="flex justify-between gap-3 border-b border-line/50 py-1">
              <dt className="text-ink-muted">{humanize(k)}</dt><dd className="font-mono text-ink">{v === null ? "n/a" : String(v)}</dd>
            </div>
          ))}
        </dl>
      </Card>
    </>
  );
}

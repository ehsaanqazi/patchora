"use client";

import clsx from "clsx";
import { Ban, FileDiff, GitPullRequest, ListPlus, RotateCcw } from "lucide-react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

import { ApprovalActions } from "@/components/ApprovalActions";
import { useApp } from "@/components/AppProviders";
import { OutcomeBadge, PolicyBadge, RiskBadge, RunStatusBadge, ValidationBadge } from "@/components/status";
import { PhaseBar, Timeline } from "@/components/Timeline";
import {
  Badge, Button, Card, ConfirmDialog, DemoBadge, ErrorState, InlineAlert, KeyValue, LinkButton, LoadingRows,
  PageHeader, SimulatedBadge,
} from "@/components/ui";
import { useAction, useApi } from "@/hooks/useApi";
import { useRunEvents } from "@/hooks/useRunEvents";
import { api, type RunDetail, type TestExecutionOut } from "@/lib/api";
import { formatDuration, humanize, isExternalUrl, shortSha, timeAgo } from "@/lib/format";

const TERMINAL = ["COMPLETED", "FAILED", "CANCELLED", "NEEDS_HUMAN_REVIEW"];
const NOT_CANCELLABLE = [...TERMINAL, "CREATING_PULL_REQUEST"];

interface Hypothesis { statement: string; evidence_ids: string[]; confidence: number }
interface Evidence { id: string; kind: string; path: string; start_line: number; end_line: number; excerpt: string; reason: string }

export default function RunDetailPage() {
  const params = useParams<{ id: string }>();
  const runId = Number(params.id);
  const run = useApi(() => api.run(runId), [runId]);
  const reloadTimer = useRef<number | undefined>(undefined);
  const scheduleReload = useCallback(() => {
    window.clearTimeout(reloadTimer.current);
    reloadTimer.current = window.setTimeout(run.reload, 300);
  }, [run.reload]);
  useEffect(() => () => window.clearTimeout(reloadTimer.current), []);
  const { events, status: streamStatus } = useRunEvents(runId, scheduleReload, Number.isFinite(runId));

  if (run.loading && !run.data) return <LoadingRows rows={10} />;
  if (run.error || !run.data) return <ErrorState error={run.error ?? new Error("Run not found")} onRetry={run.reload} />;
  return <RunView data={run.data} reload={run.reload} streamStatus={streamStatus} eventCount={events.length} />;
}

function RunView({ data, reload, streamStatus, eventCount }: {
  data: RunDetail; reload: () => void; streamStatus: string; eventCount: number;
}) {
  const router = useRouter();
  const { canReview } = useApp();
  const [confirmCancel, setConfirmCancel] = useState(false);
  const cancel = useAction(async () => { await api.cancelRun(data.id); setConfirmCancel(false); reload(); });
  const retry = useAction(async () => { const next = await api.retryRun(data.id); router.push(`/runs/${next.id}`); });
  const ghIssue = useAction(async () => { await api.createGithubIssue(data.id); reload(); });

  const patch = data.patches[data.patches.length - 1];
  const diagnosis = data.diagnosis as { error_classification?: string; hypotheses?: Hypothesis[]; uncertainty?: string;
    missing_evidence?: string[]; issue_summary?: { summary?: string } } | null;
  const evidence = (data.evidence ?? []) as unknown as Evidence[];
  const validation = data.validation as { outcome?: string; summary?: string; simulated?: boolean; reproduced?: boolean;
    backend?: string } | null;
  const finalReport = data.final_report as { limitations?: string[]; simulated_components?: string[]; diagnosis_status?: string } | null;
  const pendingApproval = data.approvals.find((a) => a.status === "pending");
  const terminal = TERMINAL.includes(data.status);
  const pr = data.pull_request as { url: string; external_pr_number: number; branch_name: string; status: string; is_demo: boolean } | null;
  const history = ((data as unknown as { validation_history?: unknown[] }).validation_history ?? []);

  return (
    <>
      <PageHeader
        title={`Run #${data.id}`}
        badge={<>
          <RunStatusBadge status={data.status} />
          <OutcomeBadge outcome={data.outcome} />
          {data.is_demo && <DemoBadge />}
          {data.llm_simulated && <SimulatedBadge what="model" />}
        </>}
        description={<Link href={`/issues/${data.sentry_issue_id}`} className="hover:text-ink">{data.sentry_issue_title}</Link>}
        actions={canReview && <>
          {patch && <LinkButton href={`/runs/${data.id}/diff`}><FileDiff className="size-4" aria-hidden />Review diff</LinkButton>}
          {!NOT_CANCELLABLE.includes(data.status) && !data.cancel_requested && (
            <Button variant="danger" icon={<Ban className="size-4" />} onClick={() => setConfirmCancel(true)}>Cancel</Button>
          )}
          {terminal && <Button icon={<RotateCcw className="size-4" />} loading={retry.pending} onClick={() => retry.run()}>Retry</Button>}
          {data.repository_id && data.diagnosis && !data.github_issues.length && (
            <Button icon={<ListPlus className="size-4" />} loading={ghIssue.pending} onClick={() => ghIssue.run()}>
              Create GitHub issue
            </Button>
          )}
        </>}
      />

      {(retry.error || ghIssue.error || cancel.error) && (
        <div className="mb-4"><ErrorState error={(retry.error ?? ghIssue.error ?? cancel.error)!} /></div>
      )}

      <Card className="mb-6">
        <div className="mb-4 flex flex-wrap items-center justify-between gap-2 text-xs">
          <span className="text-ink-muted">Current step: <span className="text-ink">{humanize(data.current_step)}</span></span>
          <span className={clsx("inline-flex items-center gap-1.5", streamStatus === "live" ? "text-ok" :
            streamStatus === "reconnecting" ? "text-warn" : "text-ink-faint")} role="status">
            <span className={clsx("size-1.5 rounded-full bg-current", streamStatus === "live" && "animate-pulse")} aria-hidden />
            {streamStatus === "live" ? `Live · ${eventCount} events` : streamStatus === "ended" ? "Up to date" : humanize(streamStatus)}
          </span>
        </div>
        <PhaseBar steps={data.steps} status={data.status} />
        {data.cancel_requested && !terminal && <div className="mt-4"><InlineAlert tone="warn">Cancellation requested; the agent stops at the next step boundary.</InlineAlert></div>}
        {data.error_message && <div className="mt-4"><InlineAlert tone={data.status === "FAILED" ? "bad" : "warn"}>{data.error_message}</InlineAlert></div>}
        {data.abstain_reasons.length > 0 && (
          <div className="mt-4"><InlineAlert tone="warn">
            <p className="font-medium">No patch was attempted:</p>
            <ul className="mt-1 list-disc pl-5">{data.abstain_reasons.map((r) => <li key={r}>{r}</li>)}</ul>
          </InlineAlert></div>
        )}
      </Card>

      {pendingApproval && (
        <Card className="mb-6 border-accent/40" title="Approval required"
          description="This patch passed sandboxed validation and policy checks. A reviewer must approve publication.">
          <ul className="mb-4 list-disc pl-5 text-sm text-ink-muted">
            {((pendingApproval.risk_summary as { reasons?: string[] }).reasons ?? []).map((r) => <li key={r}>{r}</li>)}
          </ul>
          <ApprovalActions approval={pendingApproval as never} onDone={reload} isDemo={data.is_demo} />
        </Card>
      )}

      <div className="grid gap-6 xl:grid-cols-5">
        <div className="space-y-6 xl:col-span-3">
          <Card title="Diagnosis" description="Concise, evidence-based summary. Hypotheses must cite collected evidence."
            actions={data.llm_simulated ? <SimulatedBadge what="model output" /> : data.model_name ? <Badge>{data.model_name}</Badge> : undefined}>
            {diagnosis?.hypotheses?.length ? (
              <div className="space-y-4">
                {diagnosis.issue_summary?.summary && <p className="text-sm text-ink">{diagnosis.issue_summary.summary}</p>}
                <div className="flex flex-wrap gap-2 text-xs">
                  <Badge tone={diagnosis.error_classification === "code_defect" ? "info" : "warn"}>{humanize(diagnosis.error_classification)}</Badge>
                  <Badge tone={data.diagnosis_status === "supported_by_tests" ? "ok" : "warn"}>
                    {data.diagnosis_status === "supported_by_tests" ? "Supported by a reproducing test" : "Provisional — not confirmed by a test"}
                  </Badge>
                </div>
                <ol className="space-y-3">
                  {diagnosis.hypotheses.map((h, i) => (
                    <li key={i} className="rounded-lg border border-line bg-surface-2 p-3">
                      <div className="flex items-start justify-between gap-3">
                        <p className="text-sm text-ink">{h.statement}</p>
                        <span className="shrink-0 text-xs tabular-nums text-ink-muted" title="Model-reported confidence (not proof)">
                          {(h.confidence * 100).toFixed(0)}%
                        </span>
                      </div>
                      <div className="mt-2 h-1 rounded-full bg-surface-3"><div className="h-1 rounded-full bg-accent" style={{ width: `${h.confidence * 100}%` }} /></div>
                      <p className="mt-2 text-xs text-ink-faint">Evidence: {h.evidence_ids.length ? h.evidence_ids.map((e) => (
                        <a key={e} href={`#evidence-${e}`} className="mr-1.5 font-mono text-accent-strong hover:underline">{e}</a>)) : "none cited"}</p>
                    </li>
                  ))}
                </ol>
                {diagnosis.uncertainty && <p className="text-xs text-ink-muted"><span className="text-ink">Uncertainty:</span> {diagnosis.uncertainty}</p>}
                {!!diagnosis.missing_evidence?.length && <p className="text-xs text-ink-muted"><span className="text-ink">Missing evidence:</span> {diagnosis.missing_evidence.join("; ")}</p>}
              </div>
            ) : <p className="text-sm text-ink-muted">{terminal ? "No diagnosis was produced." : "Not yet available."}</p>}
          </Card>

          <Card title="Code evidence" description={data.revision_note ?? undefined} padded={false}>
            {evidence.length ? (
              <ul className="divide-y divide-line/60">
                {evidence.map((item) => (
                  <li key={item.id} id={`evidence-${item.id}`} className="scroll-mt-20">
                    <details className="group px-5 py-3">
                      <summary className="flex cursor-pointer list-none flex-wrap items-center gap-2 text-sm">
                        <span className="font-mono text-xs text-accent-strong">{item.id}</span>
                        <span className="font-mono text-xs text-ink">{item.path}:{item.start_line}-{item.end_line}</span>
                        <Badge>{humanize(item.kind)}</Badge>
                        <span className="text-xs text-ink-faint">{item.reason}</span>
                      </summary>
                      <pre className="scrollbar-thin mt-2 max-h-80 overflow-auto rounded-lg bg-canvas p-3 font-mono text-xs leading-5 text-ink-muted">{item.excerpt}</pre>
                    </details>
                  </li>
                ))}
              </ul>
            ) : <p className="p-5 text-sm text-ink-muted">No code evidence collected yet.</p>}
            {data.investigation_notes.length > 0 && (
              <div className="border-t border-line px-5 py-3 text-xs text-ink-muted">
                <p className="mb-1 text-ink">Investigation notes</p>
                <ul className="list-disc space-y-0.5 pl-5">{data.investigation_notes.map((n) => <li key={n}>{n}</li>)}</ul>
              </div>
            )}
          </Card>

          <Card title="Tests & validation" padded={false}
            actions={validation?.outcome ? <ValidationBadge status={validation.outcome} simulated={validation.simulated} /> : undefined}>
            {validation?.summary && <p className="border-b border-line px-5 py-3 text-sm text-ink-muted">{validation.summary}</p>}
            {history.length > 1 && (
              <p className="border-b border-line px-5 py-2 text-xs text-ink-faint">{history.length} validation attempts (earlier patches were rejected by their test results).</p>
            )}
            {data.tests.length ? <TestList tests={data.tests} /> : <p className="p-5 text-sm text-ink-muted">No tests executed.</p>}
          </Card>
        </div>

        <div className="space-y-6 xl:col-span-2">
          <Card title="Run summary">
            <KeyValue items={[
              ["Repository", data.repository_full_name ?? "—"],
              ["Revision", <span key="rev" className="font-mono">{shortSha(data.source_revision)} <span className="text-ink-faint">({humanize(data.source_revision_kind)})</span></span>],
              ["Autonomy mode", humanize(data.autonomy_mode)],
              ["Trigger", humanize(data.trigger)],
              ["Duration", formatDuration(data.total_duration_ms)],
              ["Repair attempts", data.repair_attempt],
              ["Model", data.model_name ?? "—"],
              ["Tokens", data.llm_simulated ? "n/a (simulated)" : `${data.prompt_tokens} in / ${data.completion_tokens} out`],
              ["Est. cost", data.estimated_cost != null ? `$${data.estimated_cost.toFixed(4)}` : "not configured"],
              ["Created", timeAgo(data.created_at)],
            ]} />
          </Card>

          <Card title="Proposed patch" actions={patch && <LinkButton href={`/runs/${data.id}/diff`} variant="ghost">Open diff</LinkButton>}>
            {patch ? (
              <div className="space-y-3 text-sm">
                <div className="flex flex-wrap gap-2">
                  <PolicyBadge status={patch.policy_status} />
                  <ValidationBadge status={patch.validation_status} simulated={validation?.simulated} />
                  <RiskBadge risk={patch.risk_level} />
                </div>
                <p className="text-ink-muted">{String((patch.plan as { summary?: string }).summary ?? patch.diff_summary ?? "")}</p>
                <ul className="space-y-1 font-mono text-xs">
                  {(patch.files_changed as string[]).map((f) => <li key={f} className="truncate">{f}</li>)}
                </ul>
                <p className="text-xs text-ink-faint">+{patch.lines_added} / −{patch.lines_removed} lines · attempt {patch.attempt_number + 1}</p>
                {(patch.policy_findings as { code: string; message: string; path?: string }[]).length > 0 && (
                  <InlineAlert tone="bad">
                    <ul className="space-y-1">
                      {(patch.policy_findings as { code: string; message: string; path?: string }[]).map((f, i) => (
                        <li key={i}><span className="font-mono">{f.code}</span>: {f.message} {f.path && <span className="font-mono">({f.path})</span>}</li>
                      ))}
                    </ul>
                  </InlineAlert>
                )}
              </div>
            ) : <p className="text-sm text-ink-muted">No patch generated.</p>}
          </Card>

          {pr && (
            <Card title={<span className="flex items-center gap-2"><GitPullRequest className="size-4" aria-hidden />Pull request</span>}>
              <div className="space-y-2 text-sm">
                {isExternalUrl(pr.url) ? <a className="text-accent-strong hover:underline" href={pr.url} target="_blank" rel="noreferrer noopener">#{pr.external_pr_number} on GitHub</a>
                  : <p>#{pr.external_pr_number} <SimulatedBadge what="publication" /></p>}
                <p className="font-mono text-xs text-ink-muted">{pr.branch_name}</p>
                {pr.is_demo && <p className="text-xs text-ink-faint">Demo mode: the branch and PR exist only inside Patchora.</p>}
              </div>
            </Card>
          )}

          <Card title="Agent timeline">
            <Timeline steps={data.steps} />
          </Card>

          {finalReport && (
            <Card title="Final report">
              <KeyValue items={[
                ["Diagnosis", humanize(finalReport.diagnosis_status)],
                ["Simulated components", finalReport.simulated_components?.length ? finalReport.simulated_components.join(", ") : "none"],
              ]} />
              {!!finalReport.limitations?.length && (
                <ul className="mt-3 list-disc space-y-1 pl-5 text-xs text-ink-muted">{finalReport.limitations.map((l) => <li key={l}>{l}</li>)}</ul>
              )}
            </Card>
          )}
        </div>
      </div>

      <ConfirmDialog open={confirmCancel} title="Cancel this run?" confirmLabel="Cancel run" tone="danger"
        pending={cancel.pending} onCancel={() => setConfirmCancel(false)} onConfirm={() => cancel.run()}>
        <p>The agent stops at the next step boundary and cleans up its workspace. Nothing already recorded is deleted.</p>
      </ConfirmDialog>
    </>
  );
}

function TestList({ tests }: { tests: TestExecutionOut[] }) {
  return (
    <ul className="divide-y divide-line/60">
      {tests.map((t) => (
        <li key={t.id}>
          <details className="px-5 py-3">
            <summary className="flex cursor-pointer list-none flex-wrap items-center gap-2 text-sm">
              <Badge>{t.phase}</Badge>
              <ValidationBadge status={t.status} simulated={t.simulated} />
              <span className="min-w-0 flex-1 truncate font-mono text-xs text-ink-muted">{t.command_summary}</span>
              <span className="text-xs tabular-nums text-ink-faint">
                {t.tests_passed != null ? `${t.tests_passed} passed · ${t.tests_failed ?? 0} failed · ` : ""}{formatDuration(t.duration_ms)} · {t.backend}
              </span>
            </summary>
            <div className="mt-2 space-y-2">
              {t.timed_out && <InlineAlert tone="bad">Timed out — the process tree was killed.</InlineAlert>}
              {t.truncated_output && <p className="text-xs text-ink-faint">Output truncated (head and tail kept).</p>}
              <pre className="scrollbar-thin max-h-72 overflow-auto rounded-lg bg-canvas p-3 font-mono text-xs leading-5 text-ink-muted">
                {(t.stdout_summary || "") + (t.stderr_summary ? `\n--- stderr ---\n${t.stderr_summary}` : "") || "(no output)"}
              </pre>
            </div>
          </details>
        </li>
      ))}
    </ul>
  );
}

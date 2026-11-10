"use client";

import clsx from "clsx";
import { CircleCheck, CircleDashed, CircleX, Play, Sparkles } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { useApp } from "@/components/AppProviders";
import { OutcomeBadge, RunStatusBadge } from "@/components/status";
import {
  Badge, Button, Card, DemoBadge, EmptyState, ErrorState, Field, InlineAlert, LoadingRows, PageHeader, Select,
  StatCard, Table, Td, Th, Toggle,
} from "@/components/ui";
import { useAction, useApi } from "@/hooks/useApi";
import { api } from "@/lib/api";
import { humanize, timeAgo } from "@/lib/format";

export default function OverviewPage() {
  const [demo, setDemo] = useState(false);
  const { diagnostics, canReview } = useApp();
  const summary = useApi(() => api.summary(demo), [demo], 10_000);
  const runs = useApi(() => api.runs({ limit: 6, demo }), [demo], 5_000);
  const activity = useApi(() => api.activity(12), [], 10_000);
  const s = summary.data;

  return (
    <>
      <PageHeader
        title="Overview"
        description="Production errors investigated by Patchora, from Sentry issue to reviewed pull request."
        badge={demo ? <DemoBadge /> : undefined}
        actions={
          <div role="radiogroup" aria-label="Data source" className="flex rounded-lg border border-line bg-surface p-0.5 text-sm">
            {[false, true].map((value) => (
              <button key={String(value)} role="radio" aria-checked={demo === value} type="button"
                onClick={() => setDemo(value)}
                className={clsx("rounded-md px-3 py-1.5", demo === value ? "bg-surface-3 text-ink" : "text-ink-muted hover:text-ink")}>
                {value ? "Demo data" : "Real data"}
              </button>
            ))}
          </div>
        }
      />

      {demo && (
        <div className="mb-4">
          <InlineAlert tone="accent">
            Demo data is synthetic. Demo runs use scripted model responses, simulated test results and simulated
            publication — nothing is sent to GitHub, Sentry or Groq.
          </InlineAlert>
        </div>
      )}

      {summary.error ? <ErrorState error={summary.error} onRetry={summary.reload} /> : (
        <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
          <StatCard label="Issues ingested" value={s?.issues_ingested ?? "—"} href="/issues" />
          <StatCard label="Investigated" value={s?.issues_investigated ?? "—"} hint={s ? `${s.runs_total} runs` : undefined} href="/runs" />
          <StatCard label="Validated patches" value={s?.successful_validations ?? "—"} tone="ok" />
          <StatCard label="Open pull requests" value={s?.open_pull_requests ?? "—"} tone="accent" href="/pull-requests" />
          <StatCard label="Awaiting approval" value={s?.awaiting_approval ?? "—"} tone="warn" href="/approvals" />
          <StatCard label="Failed runs" value={s?.runs_failed ?? "—"} tone="bad"
            hint={s ? `${s.runs_needing_review} need review · ${s.abstentions} abstained` : undefined} />
        </div>
      )}

      <div className="mt-6 grid gap-6 xl:grid-cols-3">
        <div className="space-y-6 xl:col-span-2">
          <Card title="Recent analysis runs" padded={false} actions={<Link href="/runs" className="text-xs text-accent-strong hover:underline">View all</Link>}>
            {runs.loading && !runs.data ? <LoadingRows rows={4} /> : runs.error ? (
              <div className="p-4"><ErrorState error={runs.error} onRetry={runs.reload} /></div>
            ) : runs.data && runs.data.items.length ? (
              <Table caption="Recent analysis runs">
                <thead><tr><Th>Run</Th><Th>Issue</Th><Th>Status</Th><Th>Outcome</Th><Th>Started</Th></tr></thead>
                <tbody>
                  {runs.data.items.map((run) => (
                    <tr key={run.id} className="hover:bg-surface-2">
                      <Td><Link href={`/runs/${run.id}`} className="font-mono text-accent-strong hover:underline">#{run.id}</Link></Td>
                      <Td className="max-w-xs"><span className="line-clamp-1">{run.sentry_issue_title}</span></Td>
                      <Td><RunStatusBadge status={run.status} /></Td>
                      <Td><OutcomeBadge outcome={run.outcome} /></Td>
                      <Td className="whitespace-nowrap text-ink-muted">{timeAgo(run.created_at)}</Td>
                    </tr>
                  ))}
                </tbody>
              </Table>
            ) : (
              <EmptyState title="No runs yet"
                description={demo ? "Start a demo scenario to see the full workflow." :
                  "Start an investigation from the Sentry Issues page, or enable webhooks."} />
            )}
          </Card>
          {canReview && <DemoLauncher />}
        </div>

        <div className="space-y-6">
          <Card title="Integrations">
            {diagnostics ? (
              <ul className="space-y-3 text-sm">
                {(["groq", "sentry", "github"] as const).map((name) => {
                  const item = diagnostics.integrations[name];
                  const Icon = item?.configured ? (item.last_ok === false ? CircleX : CircleCheck) : CircleDashed;
                  return (
                    <li key={name} className="flex items-center justify-between">
                      <span className="flex items-center gap-2 capitalize">
                        <Icon className={clsx("size-4", item?.configured ? (item.last_ok === false ? "text-bad" : "text-ok") : "text-ink-faint")} aria-hidden />
                        {name}
                      </span>
                      <Badge tone={item?.configured ? "ok" : "neutral"}>{item?.configured ? (item.mode ?? "configured") : "not configured"}</Badge>
                    </li>
                  );
                })}
                <li className="flex items-center justify-between border-t border-line pt-3">
                  <span>Sandboxed execution</span>
                  <Badge tone={diagnostics.sandbox.code_execution_enabled ? "ok" : "warn"} title={diagnostics.sandbox.detail}>
                    {diagnostics.sandbox.code_execution_enabled ? diagnostics.sandbox.backend : "unavailable"}
                  </Badge>
                </li>
              </ul>
            ) : <LoadingRows rows={3} />}
            <Link href="/integrations" className="mt-4 inline-block text-xs text-accent-strong hover:underline">Manage integrations</Link>
          </Card>

          <Card title="Recent activity" description="Append-only audit trail" padded={false}>
            {activity.data && activity.data.length ? (
              <ol className="divide-y divide-line/60">
                {activity.data.map((item) => (
                  <li key={item.id} className="px-5 py-2.5 text-xs">
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-medium text-ink">{humanize(item.action.replace(".", " "))}</span>
                      <span className="text-ink-faint">{timeAgo(item.created_at)}</span>
                    </div>
                    <p className="mt-0.5 truncate text-ink-faint">{item.actor} → {item.resource}</p>
                  </li>
                ))}
              </ol>
            ) : activity.loading ? <LoadingRows rows={4} /> : <EmptyState title="No activity yet" />}
          </Card>
        </div>
      </div>
    </>
  );
}

function DemoLauncher() {
  const router = useRouter();
  const scenarios = useApi(() => api.demoScenarios(), []);
  const [scenario, setScenario] = useState("zero-division");
  const [mode, setMode] = useState("prepare_patch");
  const [realLlm, setRealLlm] = useState(false);
  const { diagnostics } = useApp();
  const start = useAction(async () => {
    const result = await api.startDemo(scenario, mode, realLlm);
    router.push(`/runs/${result.run_id}`);
  });
  const groqReady = !!diagnostics?.integrations.groq?.configured;
  const selected = scenarios.data?.find((item) => item.key === scenario);

  return (
    <Card title={<span className="flex items-center gap-2"><Sparkles className="size-4 text-accent-strong" aria-hidden />Run a demo scenario</span>}
      description="Synthetic Sentry issues against a bundled fixture repository. Works without any credentials.">
      <div className="grid gap-4 md:grid-cols-2">
        <Field label="Scenario" hint={selected?.description}>
          {(id) => (
            <Select id={id} value={scenario} onChange={(e) => setScenario(e.target.value)}>
              {scenarios.data?.map((item) => <option key={item.key} value={item.key}>{item.short_id} · {item.issue_title}</option>)}
            </Select>
          )}
        </Field>
        <Field label="Autonomy mode" hint="Mode B (prepare patch, wait for approval) is the default.">
          {(id) => (
            <Select id={id} value={mode} onChange={(e) => setMode(e.target.value)}>
              <option value="analyze_only">A · Analyze only</option>
              <option value="prepare_patch">B · Prepare patch (approval required)</option>
              <option value="auto_pr">C · Automatic PR after policy checks</option>
            </Select>
          )}
        </Field>
      </div>
      <div className="mt-4">
        <Toggle checked={realLlm} onChange={setRealLlm} disabled={!groqReady}
          label="Use the real Groq model"
          description={groqReady ? "Diagnosis and patch come from Groq; tests run in the configured sandbox (not simulated)."
            : "Configure Groq on the Integrations page to enable."} />
      </div>
      {start.error && <div className="mt-3"><ErrorState error={start.error} /></div>}
      <div className="mt-4 flex justify-end">
        <Button variant="primary" icon={<Play className="size-4" />} loading={start.pending} onClick={() => start.run()}>
          Start demo run
        </Button>
      </div>
    </Card>
  );
}

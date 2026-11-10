"use client";

import { ExternalLink, Play, RotateCcw } from "lucide-react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useState } from "react";

import { useApp } from "@/components/AppProviders";
import { StackTrace } from "@/components/StackTrace";
import { LevelBadge, OutcomeBadge, RunStatusBadge } from "@/components/status";
import {
  Badge, Button, Card, ConfirmDialog, DemoBadge, EmptyState, ErrorState, Field, InlineAlert, KeyValue, LoadingRows,
  PageHeader, Select, Table, Td, Th,
} from "@/components/ui";
import { useAction, useApi } from "@/hooks/useApi";
import { api, type RunOut } from "@/lib/api";
import { formatDate, humanize, isExternalUrl, timeAgo } from "@/lib/format";

const MODES: Record<string, string> = {
  analyze_only: "A · Analyze only — diagnosis and patch preview; nothing is executed or published.",
  prepare_patch: "B · Prepare patch — run tests in the sandbox, then wait for explicit approval before publishing.",
  auto_pr: "C · Automatic PR — publish a draft PR only if every policy check passes (never merges).",
};

export default function IssueDetailPage() {
  const params = useParams<{ id: string }>();
  const id = Number(params.id);
  const router = useRouter();
  const { canReview, diagnostics } = useApp();
  const issue = useApi(() => api.issue(id), [id]);
  const repos = useApi(() => api.repositories(), []);
  const [confirm, setConfirm] = useState(false);
  const [mode, setMode] = useState(diagnostics?.autonomy_mode ?? "prepare_patch");
  const analyze = useAction(async () => {
    const run = await api.analyzeIssue(id, { autonomy_mode: mode });
    router.push(`/runs/${run.id}`);
  });
  const link = useAction(async (repositoryId: number | null) => {
    await api.linkRepository(id, repositoryId);
    issue.reload();
  });

  if (issue.loading && !issue.data) return <LoadingRows rows={8} />;
  if (issue.error) return <ErrorState error={issue.error} onRetry={issue.reload} />;
  const data = issue.data!;
  const runs = (data.runs ?? []) as unknown as RunOut[];
  const activeRun = runs.find((r) => !["COMPLETED", "FAILED", "CANCELLED", "NEEDS_HUMAN_REVIEW"].includes(r.status));
  const eligibleRepos = (repos.data?.items ?? []).filter((r) => r.is_demo === data.is_demo);

  return (
    <>
      <PageHeader
        title={data.title}
        badge={<>{data.is_demo && <DemoBadge />}<LevelBadge level={data.level} /></>}
        description={<span className="font-mono">{data.short_id ?? data.sentry_issue_id} · {data.culprit ?? "no culprit"}</span>}
        actions={
          <>
            {isExternalUrl(data.permalink) && (
              <a href={data.permalink} target="_blank" rel="noreferrer noopener"
                className="inline-flex h-9 items-center gap-2 rounded-lg border border-line-strong bg-surface-3 px-4 text-sm">
                <ExternalLink className="size-4" aria-hidden />Open in Sentry
              </a>
            )}
            {canReview && (activeRun ? (
              <Link href={`/runs/${activeRun.id}`} className="inline-flex h-9 items-center rounded-lg bg-accent px-4 text-sm font-medium text-white">
                View active run #{activeRun.id}
              </Link>
            ) : (
              <Button variant="primary" icon={runs.length ? <RotateCcw className="size-4" /> : <Play className="size-4" />}
                onClick={() => setConfirm(true)}>
                {runs.length ? "Investigate again" : "Start investigation"}
              </Button>
            ))}
          </>
        }
      />

      <div className="grid gap-6 xl:grid-cols-3">
        <div className="space-y-6 xl:col-span-2">
          <Card title="Exception & stack trace" description={data.event ? `Latest event ${data.event.event_id}` : undefined}>
            {data.event ? <StackTrace event={data.event} /> : data.event_error ? (
              <InlineAlert tone="warn">Could not load the latest event: {data.event_error}</InlineAlert>
            ) : <p className="text-sm text-ink-muted">No event data available.</p>}
          </Card>

          <Card title="Investigation history" padded={false}>
            {runs.length ? (
              <Table caption="Previous investigation attempts">
                <thead><tr><Th>Run</Th><Th>Status</Th><Th>Outcome</Th><Th>Mode</Th><Th>Root cause</Th><Th>When</Th></tr></thead>
                <tbody>
                  {runs.map((run) => (
                    <tr key={run.id} className="hover:bg-surface-2">
                      <Td><Link className="font-mono text-accent-strong hover:underline" href={`/runs/${run.id}`}>#{run.id}</Link></Td>
                      <Td><RunStatusBadge status={run.status} /></Td>
                      <Td><OutcomeBadge outcome={run.outcome} /></Td>
                      <Td className="text-xs text-ink-muted">{humanize(run.autonomy_mode)}</Td>
                      <Td className="max-w-sm text-xs text-ink-muted"><span className="line-clamp-2">{run.root_cause_summary ?? "—"}</span></Td>
                      <Td className="whitespace-nowrap text-xs text-ink-faint">{timeAgo(run.created_at)}</Td>
                    </tr>
                  ))}
                </tbody>
              </Table>
            ) : <EmptyState title="Not investigated yet" description="Start an investigation to diagnose this error." />}
          </Card>
        </div>

        <div className="space-y-6">
          <Card title="Details">
            <KeyValue items={[
              ["Project", `${data.organization}/${data.project}`],
              ["Exception", data.exception_type ?? "—"],
              ["Status", data.status ?? "—"],
              ["Events", data.event_count],
              ["First seen", formatDate(data.first_seen)],
              ["Last seen", formatDate(data.last_seen)],
              ["Release", data.release ?? "unknown"],
              ["Environment", data.event?.environment ?? "—"],
            ]} />
          </Card>

          <Card title="Repository mapping" description="The mapping — never a model guess — decides which repository is investigated.">
            {data.mapping ? (
              <KeyValue items={[
                ["Mapped project", `${data.mapping.sentry_organization}/${data.mapping.sentry_project}`],
                ["Branch", data.mapping.branch ?? "default"],
                ["Strip prefixes", data.mapping.strip_prefixes.join(", ") || "—"],
                ["Path prefixes", data.mapping.path_prefixes.join(", ") || "—"],
              ]} />
            ) : <InlineAlert tone="warn">No project mapping. Add one on the Repositories page, or link a repository below.</InlineAlert>}
            <div className="mt-4">
              <Field label="Linked repository">
                {(fieldId) => (
                  <Select id={fieldId} value={data.repository_id ?? ""} disabled={!canReview || link.pending}
                    onChange={(e) => link.run(e.target.value ? Number(e.target.value) : null)}>
                    <option value="">Not linked</option>
                    {eligibleRepos.map((r) => <option key={r.id} value={r.id}>{r.full_name}{r.enabled ? "" : " (not allowlisted)"}</option>)}
                  </Select>
                )}
              </Field>
              {link.error && <p className="mt-2 text-xs text-bad">{link.error.message}</p>}
            </div>
          </Card>

          <Card title="GitHub issues">
            {data.github_issues?.length ? (
              <ul className="space-y-2 text-sm">
                {data.github_issues.map((g) => (
                  <li key={String(g.id)} className="flex items-center justify-between gap-2">
                    {isExternalUrl(String(g.url)) ? <a className="truncate text-accent-strong hover:underline" href={String(g.url)} target="_blank" rel="noreferrer noopener">#{String(g.number)} {String(g.title)}</a>
                      : <span className="truncate">#{String(g.number)} {String(g.title)}</span>}
                    {g.is_demo ? <Badge tone="warn">simulated</Badge> : <Badge>{String(g.status)}</Badge>}
                  </li>
                ))}
              </ul>
            ) : <p className="text-sm text-ink-muted">None linked. Create one from a run page.</p>}
          </Card>
        </div>
      </div>

      <ConfirmDialog open={confirm} title="Start investigation" confirmLabel="Start" pending={analyze.pending}
        onCancel={() => setConfirm(false)} onConfirm={() => analyze.run()}>
        <Field label="Autonomy mode">
          {(fieldId) => (
            <Select id={fieldId} value={mode} onChange={(e) => setMode(e.target.value)}>
              {Object.keys(MODES).map((key) => <option key={key} value={key}>{key.replace(/_/g, " ")}</option>)}
            </Select>
          )}
        </Field>
        <p>{MODES[mode]}</p>
        <p className="text-xs text-ink-faint">The agent never pushes to the default branch, merges or deploys.</p>
        {analyze.error && <ErrorState error={analyze.error} />}
      </ConfirmDialog>
    </>
  );
}

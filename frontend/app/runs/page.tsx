"use client";

import Link from "next/link";
import { useState } from "react";

import { OutcomeBadge, RunStatusBadge } from "@/components/status";
import {
  Badge, Card, DemoBadge, EmptyState, ErrorState, LoadingRows, PageHeader, Pagination, Select, Table, Td, Th,
} from "@/components/ui";
import { useApi } from "@/hooks/useApi";
import { api } from "@/lib/api";
import { formatDuration, humanize, timeAgo } from "@/lib/format";

const LIMIT = 20;
const STATUSES = ["WAITING_FOR_APPROVAL", "NEEDS_HUMAN_REVIEW", "COMPLETED", "FAILED", "CANCELLED", "RUNNING_TESTS",
  "INVESTIGATING"];

export default function RunsPage() {
  const [status, setStatus] = useState("");
  const [source, setSource] = useState("all");
  const [offset, setOffset] = useState(0);
  const demo = source === "all" ? undefined : source === "demo";
  const runs = useApi(() => api.runs({ status, demo, limit: LIMIT, offset }), [status, source, offset], 5000);

  return (
    <>
      <PageHeader title="Agent Runs" description="Every investigation, its current state and outcome. Updates automatically." />
      <Card padded={false}>
        <div className="flex flex-wrap gap-3 border-b border-line p-4">
          <Select aria-label="Status" value={status} onChange={(e) => { setStatus(e.target.value); setOffset(0); }} className="w-56">
            <option value="">All statuses</option>
            {STATUSES.map((s) => <option key={s} value={s}>{humanize(s)}</option>)}
          </Select>
          <Select aria-label="Data source" value={source} onChange={(e) => { setSource(e.target.value); setOffset(0); }} className="w-36">
            <option value="all">All data</option><option value="real">Real only</option><option value="demo">Demo only</option>
          </Select>
        </div>
        {runs.loading && !runs.data ? <LoadingRows /> : runs.error ? (
          <div className="p-4"><ErrorState error={runs.error} onRetry={runs.reload} /></div>
        ) : runs.data && runs.data.items.length ? (
          <>
            <Table caption="Analysis runs">
              <thead><tr><Th>Run</Th><Th>Issue</Th><Th>Repository</Th><Th>Status</Th><Th>Outcome</Th><Th>Mode</Th><Th>Duration</Th><Th>Created</Th></tr></thead>
              <tbody>
                {runs.data.items.map((run) => (
                  <tr key={run.id} className="hover:bg-surface-2">
                    <Td>
                      <Link href={`/runs/${run.id}`} className="font-mono text-accent-strong hover:underline">#{run.id}</Link>
                      <div className="mt-1 flex gap-1">{run.is_demo && <DemoBadge />}{run.retry_of_run_id && <Badge>retry of #{run.retry_of_run_id}</Badge>}</div>
                    </Td>
                    <Td className="max-w-xs"><span className="line-clamp-2">{run.sentry_issue_title}</span></Td>
                    <Td className="text-xs text-ink-muted">{run.repository_full_name ?? "—"}</Td>
                    <Td><RunStatusBadge status={run.status} /></Td>
                    <Td><OutcomeBadge outcome={run.outcome} /></Td>
                    <Td className="text-xs text-ink-muted">{humanize(run.autonomy_mode)}</Td>
                    <Td className="text-xs tabular-nums text-ink-muted">{formatDuration(run.total_duration_ms)}</Td>
                    <Td className="whitespace-nowrap text-xs text-ink-faint">{timeAgo(run.created_at)}</Td>
                  </tr>
                ))}
              </tbody>
            </Table>
            <Pagination total={runs.data.total} limit={LIMIT} offset={offset} onChange={setOffset} />
          </>
        ) : <EmptyState title="No runs" description="Start an investigation from an issue, or run a demo scenario from the Overview." />}
      </Card>
    </>
  );
}

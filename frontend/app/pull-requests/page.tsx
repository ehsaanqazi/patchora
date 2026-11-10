"use client";

import { ExternalLink, RefreshCw } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { PrStatusBadge, ValidationBadge } from "@/components/status";
import {
  Button, Card, DemoBadge, EmptyState, ErrorState, LoadingRows, PageHeader, Pagination, Select, SimulatedBadge, Table,
  Td, Th,
} from "@/components/ui";
import { useAction, useApi } from "@/hooks/useApi";
import { api } from "@/lib/api";
import { isExternalUrl, timeAgo } from "@/lib/format";

const LIMIT = 20;

export default function PullRequestsPage() {
  const [status, setStatus] = useState("");
  const [offset, setOffset] = useState(0);
  const prs = useApi(() => api.pullRequests({ status, limit: LIMIT, offset }), [status, offset]);
  const refresh = useAction(async (id: number) => { await api.refreshPullRequest(id); prs.reload(); });

  return (
    <>
      <PageHeader title="Pull Requests"
        description="Draft pull requests opened by Patchora for human review. Patchora never merges or deploys." />
      <Card padded={false}>
        <div className="border-b border-line p-4">
          <Select aria-label="Status" value={status} className="w-40" onChange={(e) => { setStatus(e.target.value); setOffset(0); }}>
            <option value="">All</option><option value="open">Open</option><option value="closed">Closed</option><option value="merged">Merged</option>
          </Select>
        </div>
        {prs.loading && !prs.data ? <LoadingRows /> : prs.error ? <div className="p-4"><ErrorState error={prs.error} onRetry={prs.reload} /></div>
          : prs.data && prs.data.items.length ? (
            <>
              <Table caption="Pull requests">
                <thead><tr><Th>Pull request</Th><Th>Repository</Th><Th>Branch</Th><Th>Status</Th><Th>Validation</Th><Th>Sentry issue</Th><Th>Created</Th><Th><span className="sr-only">Actions</span></Th></tr></thead>
                <tbody>
                  {prs.data.items.map((pr) => (
                    <tr key={pr.id} className="hover:bg-surface-2">
                      <Td className="max-w-sm">
                        <p className="line-clamp-2 text-ink">{pr.title}</p>
                        <div className="mt-1 flex items-center gap-2 text-xs">
                          <Link href={`/runs/${pr.run_id}`} className="text-accent-strong hover:underline">run #{pr.run_id}</Link>
                          {pr.is_demo && <><DemoBadge /><SimulatedBadge what="publication" /></>}
                        </div>
                      </Td>
                      <Td className="font-mono text-xs">{pr.repository_full_name} #{pr.external_pr_number}</Td>
                      <Td className="max-w-[220px] truncate font-mono text-xs text-ink-muted" >{pr.branch_name} → {pr.base_branch}</Td>
                      <Td><PrStatusBadge status={pr.status} draft={pr.draft} /></Td>
                      <Td><ValidationBadge status={pr.validation_outcome} /></Td>
                      <Td className="max-w-xs text-xs">
                        {pr.sentry_issue_id ? <Link href={`/issues/${pr.sentry_issue_id}`} className="line-clamp-2 hover:text-accent-strong">{pr.sentry_issue_title}</Link> : "—"}
                      </Td>
                      <Td className="whitespace-nowrap text-xs text-ink-faint">{timeAgo(pr.created_at)}</Td>
                      <Td>
                        <div className="flex items-center gap-1">
                          {isExternalUrl(pr.url) && (
                            <a href={pr.url} target="_blank" rel="noreferrer noopener" aria-label="Open on GitHub" className="rounded-md p-1.5 text-ink-faint hover:text-ink">
                              <ExternalLink className="size-4" />
                            </a>
                          )}
                          {!pr.is_demo && <Button size="sm" variant="ghost" aria-label="Refresh status" icon={<RefreshCw className="size-3.5" />}
                            onClick={() => refresh.run(pr.id)} />}
                        </div>
                      </Td>
                    </tr>
                  ))}
                </tbody>
              </Table>
              <Pagination total={prs.data.total} limit={LIMIT} offset={offset} onChange={setOffset} />
            </>
          ) : <EmptyState title="No pull requests yet" description="Approved, validated patches appear here once published." />}
        {refresh.error && <div className="p-4"><ErrorState error={refresh.error} /></div>}
      </Card>
    </>
  );
}

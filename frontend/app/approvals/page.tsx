"use client";

import { FileDiff } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { ApprovalActions } from "@/components/ApprovalActions";
import { RiskBadge, ValidationBadge } from "@/components/status";
import { Badge, Card, DemoBadge, EmptyState, ErrorState, LoadingRows, PageHeader, Select } from "@/components/ui";
import { useApi } from "@/hooks/useApi";
import { api } from "@/lib/api";
import { humanize, timeAgo } from "@/lib/format";

export default function ApprovalsPage() {
  const [status, setStatus] = useState("pending");
  const approvals = useApi(() => api.approvals({ status, limit: 50 }), [status], 10_000);
  return (
    <>
      <PageHeader title="Approvals"
        description="Validated patches waiting for a human decision. Approving publishes a branch and draft pull request; it never merges."
        actions={
          <Select aria-label="Status" value={status} className="w-40" onChange={(e) => setStatus(e.target.value)}>
            <option value="pending">Pending</option><option value="approved">Approved</option>
            <option value="rejected">Rejected</option><option value="expired">Expired</option><option value="">All</option>
          </Select>
        } />
      {approvals.loading && !approvals.data ? <LoadingRows /> : approvals.error ? <ErrorState error={approvals.error} onRetry={approvals.reload} />
        : approvals.data && approvals.data.items.length ? (
          <div className="space-y-4">
            {approvals.data.items.map((a) => {
              const risk = a.risk_summary as { risk?: string; reasons?: string[]; validation?: string; reproduced?: boolean };
              return (
                <Card key={a.id}
                  title={<span className="flex flex-wrap items-center gap-2">
                    <Link href={`/runs/${a.run_id}`} className="hover:text-accent-strong">{a.issue_title ?? `Run #${a.run_id}`}</Link>
                    {a.is_demo && <DemoBadge />}
                  </span>}
                  description={`${a.repository_full_name ?? "—"} · run #${a.run_id} · requested ${timeAgo(a.created_at)}`}
                  actions={<Link href={`/runs/${a.run_id}/diff`} className="inline-flex items-center gap-1 text-xs text-accent-strong hover:underline">
                    <FileDiff className="size-3.5" aria-hidden />Review diff</Link>}>
                  <div className="flex flex-wrap gap-2">
                    <Badge tone={a.status === "pending" ? "accent" : a.status === "approved" ? "ok" : "neutral"}>{humanize(a.status)}</Badge>
                    <RiskBadge risk={risk.risk} />
                    <ValidationBadge status={risk.validation} />
                    <Badge tone={risk.reproduced ? "ok" : "warn"}>{risk.reproduced ? "Failure reproduced by a test" : "Not reproduced by a test"}</Badge>
                  </div>
                  <p className="mt-3 font-mono text-xs text-ink-muted">{a.files_changed.join(", ")}</p>
                  {!!risk.reasons?.length && <ul className="mt-3 list-disc pl-5 text-sm text-ink-muted">{risk.reasons.map((r) => <li key={r}>{r}</li>)}</ul>}
                  {a.status !== "pending" && (
                    <p className="mt-3 text-xs text-ink-faint">{humanize(a.status)} by {a.reviewer_name ?? "—"} {timeAgo(a.resolved_at)}{a.reason ? ` — “${a.reason}”` : ""}</p>
                  )}
                  <div className="mt-4"><ApprovalActions approval={a} onDone={approvals.reload} isDemo={a.is_demo} /></div>
                </Card>
              );
            })}
          </div>
        ) : <Card><EmptyState title={status === "pending" ? "Nothing waiting for approval" : "No approvals"} /></Card>}
    </>
  );
}

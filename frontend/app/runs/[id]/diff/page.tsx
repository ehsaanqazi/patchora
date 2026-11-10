"use client";

import { ArrowLeft, ShieldCheck } from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useMemo, useState } from "react";

import { ApprovalActions } from "@/components/ApprovalActions";
import { DiffFileList, DiffViewer, parseDiff } from "@/components/DiffViewer";
import { PolicyBadge, RiskBadge, ValidationBadge } from "@/components/status";
import {
  Button, Card, DemoBadge, ErrorState, InlineAlert, LoadingRows, PageHeader, Select, SimulatedBadge,
} from "@/components/ui";
import { useAction, useApi } from "@/hooks/useApi";
import { api, type PolicyReportOut } from "@/lib/api";
import { isExternalUrl } from "@/lib/format";

export default function DiffPage() {
  const params = useParams<{ id: string }>();
  const runId = Number(params.id);
  const run = useApi(() => api.run(runId), [runId]);
  const [attempt, setAttempt] = useState<number | undefined>(undefined);
  const diff = useApi(() => api.diff(runId, attempt), [runId, attempt]);
  const [report, setReport] = useState<PolicyReportOut>();
  const revalidate = useAction(async () => setReport(await api.validatePatch(runId)));
  const files = useMemo(() => parseDiff(diff.data ?? ""), [diff.data]);

  if (run.loading && !run.data) return <LoadingRows rows={8} />;
  if (run.error || !run.data) return <ErrorState error={run.error ?? new Error("Not found")} onRetry={run.reload} />;
  const data = run.data;
  const patches = data.patches;
  const patch = patches.find((p) => p.attempt_number === attempt) ?? patches[patches.length - 1];
  const validation = data.validation as { simulated?: boolean; summary?: string } | null;
  const pending = data.approvals.find((a) => a.status === "pending" && a.patch_id === patch?.id);
  const pr = data.pull_request as { url: string; external_pr_number: number } | null;
  const tests = data.tests.filter((t) => t.patch_id === patch?.id);
  const findings = (report?.findings ?? patch?.policy_findings ?? []) as { code: string; severity: string; message: string; path?: string }[];

  return (
    <>
      <Link href={`/runs/${runId}`} className="mb-3 inline-flex items-center gap-1 text-xs text-ink-muted hover:text-ink">
        <ArrowLeft className="size-3.5" aria-hidden />Run #{runId}
      </Link>
      <PageHeader title="Diff review" description={data.sentry_issue_title}
        badge={<>{data.is_demo && <DemoBadge />}{data.llm_simulated && <SimulatedBadge what="patch" />}</>}
        actions={patches.length > 1 && (
          <Select aria-label="Patch attempt" value={patch?.attempt_number ?? ""} className="w-48"
            onChange={(e) => setAttempt(Number(e.target.value))}>
            {patches.map((p) => <option key={p.id} value={p.attempt_number}>Attempt {p.attempt_number + 1}{p.superseded ? " (superseded)" : ""}</option>)}
          </Select>
        )}
      />

      {!patch ? <InlineAlert>No patch was generated for this run.</InlineAlert> : (
        <div className="grid gap-6 xl:grid-cols-4">
          <div className="space-y-6 xl:col-span-1">
            <Card title="Files">
              <DiffFileList files={files} />
              <p className="mt-3 text-xs text-ink-faint">+{patch.lines_added} / −{patch.lines_removed} · base <span className="font-mono">{patch.base_commit_sha?.slice(0, 12)}</span></p>
            </Card>
            <Card title="Checks">
              <div className="flex flex-wrap gap-2">
                <PolicyBadge status={patch.policy_status} />
                <ValidationBadge status={patch.validation_status} simulated={validation?.simulated} />
                <RiskBadge risk={patch.risk_level} />
              </div>
              {validation?.summary && <p className="mt-3 text-xs text-ink-muted">{validation.summary}</p>}
              <ul className="mt-3 space-y-1 text-xs text-ink-muted">
                {tests.map((t) => <li key={t.id}><span className="text-ink">{t.phase}</span> · {t.status}{t.tests_passed != null ? ` · ${t.tests_passed} passed / ${t.tests_failed ?? 0} failed` : ""}</li>)}
                {!tests.length && <li>No tests executed for this attempt.</li>}
              </ul>
              <Button size="sm" className="mt-4" icon={<ShieldCheck className="size-3.5" />} loading={revalidate.pending} onClick={() => revalidate.run()}>
                Re-check policy
              </Button>
              {revalidate.error && <div className="mt-2"><ErrorState error={revalidate.error} /></div>}
            </Card>
            {findings.length > 0 && (
              <Card title="Risk warnings">
                <ul className="space-y-2 text-xs">
                  {findings.map((f, i) => (
                    <li key={i} className={f.severity === "block" ? "text-bad" : "text-warn"}>
                      <span className="font-mono">{f.code}</span> {f.message} {f.path && <span className="font-mono">({f.path})</span>}
                    </li>
                  ))}
                </ul>
              </Card>
            )}
            {report && !report.blocked && <InlineAlert tone="ok">Policy re-check passed with current settings (risk {report.risk_level}).</InlineAlert>}
            {pending && (
              <Card title="Decision">
                <ApprovalActions approval={pending as never} onDone={run.reload} isDemo={data.is_demo} />
              </Card>
            )}
            {pr && (
              <Card title="Pull request">
                {isExternalUrl(pr.url) ? <a href={pr.url} className="text-sm text-accent-strong hover:underline" target="_blank" rel="noreferrer noopener">#{pr.external_pr_number} on GitHub</a>
                  : <p className="text-sm">#{pr.external_pr_number} <SimulatedBadge what="publication" /></p>}
              </Card>
            )}
          </div>
          <div className="min-w-0 xl:col-span-3">
            {diff.loading && !diff.data ? <LoadingRows rows={10} /> : diff.error ? <ErrorState error={diff.error} onRetry={diff.reload} /> : <DiffViewer diff={diff.data ?? ""} />}
          </div>
        </div>
      )}
    </>
  );
}

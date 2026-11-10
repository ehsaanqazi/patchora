"use client";

import { Download, ExternalLink, Search } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { useApp } from "@/components/AppProviders";
import { LevelBadge, RunStatusBadge } from "@/components/status";
import {
  Badge, Button, Card, DemoBadge, EmptyState, ErrorState, Input, LinkButton, LoadingRows, PageHeader, Pagination,
  Select, Table, Td, Th,
} from "@/components/ui";
import { useAction, useApi } from "@/hooks/useApi";
import { api } from "@/lib/api";
import { formatDate, isExternalUrl, timeAgo } from "@/lib/format";

const LIMIT = 20;

export default function IssuesPage() {
  const [q, setQ] = useState("");
  const [search, setSearch] = useState("");
  const [level, setLevel] = useState("");
  const [status, setStatus] = useState("");
  const [source, setSource] = useState("all");
  const [sort, setSort] = useState("last_seen");
  const [offset, setOffset] = useState(0);
  const { canReview } = useApp();
  const demo = source === "all" ? undefined : source === "demo";
  const issues = useApi(() => api.issues({ q: search, level, status, demo, sort, limit: LIMIT, offset }),
    [search, level, status, source, sort, offset]);

  const reset = (fn: () => void) => { fn(); setOffset(0); };

  return (
    <>
      <PageHeader title="Sentry Issues"
        description="Issues ingested from Sentry (webhooks or manual import). Demo issues are synthetic and labeled." />

      <Card padded={false}>
        <form className="flex flex-wrap items-end gap-3 border-b border-line p-4" role="search"
          onSubmit={(e) => { e.preventDefault(); reset(() => setSearch(q)); }}>
          <div className="relative min-w-[220px] flex-1">
            <Search className="pointer-events-none absolute left-3 top-2.5 size-4 text-ink-faint" aria-hidden />
            <Input aria-label="Search issues" placeholder="Search title, culprit or short id" value={q}
              onChange={(e) => setQ(e.target.value)} className="pl-9" />
          </div>
          <Select aria-label="Level" value={level} onChange={(e) => reset(() => setLevel(e.target.value))} className="w-36">
            <option value="">All levels</option>
            {["fatal", "error", "warning", "info"].map((l) => <option key={l} value={l}>{l}</option>)}
          </Select>
          <Select aria-label="Status" value={status} onChange={(e) => reset(() => setStatus(e.target.value))} className="w-40">
            <option value="">Any status</option>
            {["unresolved", "resolved", "ignored"].map((s) => <option key={s} value={s}>{s}</option>)}
          </Select>
          <Select aria-label="Data source" value={source} onChange={(e) => reset(() => setSource(e.target.value))} className="w-36">
            <option value="all">All data</option><option value="real">Real only</option><option value="demo">Demo only</option>
          </Select>
          <Select aria-label="Sort by" value={sort} onChange={(e) => reset(() => setSort(e.target.value))} className="w-40">
            <option value="last_seen">Last seen</option><option value="first_seen">First seen</option>
            <option value="events">Most events</option><option value="created">Recently ingested</option>
          </Select>
          <Button type="submit" variant="secondary">Search</Button>
        </form>

        {issues.loading && !issues.data ? <LoadingRows /> : issues.error ? (
          <div className="p-4"><ErrorState error={issues.error} onRetry={issues.reload} /></div>
        ) : issues.data && issues.data.items.length ? (
          <>
            <Table caption="Sentry issues">
              <thead>
                <tr><Th>Issue</Th><Th>Level</Th><Th>Project</Th><Th>Seen</Th><Th>Events</Th><Th>Repository</Th><Th>Investigation</Th><Th><span className="sr-only">Links</span></Th></tr>
              </thead>
              <tbody>
                {issues.data.items.map((issue) => (
                  <tr key={issue.id} className="hover:bg-surface-2">
                    <Td className="max-w-md">
                      <Link href={`/issues/${issue.id}`} className="font-medium text-ink hover:text-accent-strong">
                        <span className="line-clamp-2">{issue.title}</span>
                      </Link>
                      <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-ink-faint">
                        <span className="font-mono">{issue.short_id ?? issue.sentry_issue_id}</span>
                        {issue.exception_type && <span>{issue.exception_type}</span>}
                        {issue.is_demo && <DemoBadge />}
                      </div>
                    </Td>
                    <Td><LevelBadge level={issue.level} /></Td>
                    <Td className="text-ink-muted">{issue.project}</Td>
                    <Td className="whitespace-nowrap text-xs text-ink-muted">
                      <div title={formatDate(issue.last_seen)}>last {timeAgo(issue.last_seen)}</div>
                      <div title={formatDate(issue.first_seen)}>first {timeAgo(issue.first_seen)}</div>
                    </Td>
                    <Td className="tabular-nums">{issue.event_count}</Td>
                    <Td className="text-xs">{issue.repository_full_name ?? <Badge tone="warn">unmapped</Badge>}</Td>
                    <Td>
                      {issue.latest_run_id ? (
                        <Link href={`/runs/${issue.latest_run_id}`}><RunStatusBadge status={issue.latest_run_status ?? ""} /></Link>
                      ) : <span className="text-xs text-ink-faint">Not investigated</span>}
                    </Td>
                    <Td>
                      {isExternalUrl(issue.permalink) && (
                        <a href={issue.permalink} target="_blank" rel="noreferrer noopener" aria-label="Open in Sentry"
                          className="text-ink-faint hover:text-ink"><ExternalLink className="size-4" /></a>
                      )}
                    </Td>
                  </tr>
                ))}
              </tbody>
            </Table>
            <Pagination total={issues.data.total} limit={LIMIT} offset={offset} onChange={setOffset} />
          </>
        ) : (
          <EmptyState title="No issues found"
            description="Configure the Sentry webhook, import issues below, or seed demo data from the Overview page."
            action={<LinkButton href="/">Go to Overview</LinkButton>} />
        )}
      </Card>

      {canReview && <RemoteIssues onImported={issues.reload} />}
    </>
  );
}

function RemoteIssues({ onImported }: { onImported: () => void }) {
  const [enabled, setEnabled] = useState(false);
  const remote = useApi(() => (enabled ? api.remoteIssues({}) : Promise.resolve(undefined)), [enabled]);
  const importIssue = useAction(async (id: string) => {
    await api.importIssue(id);
    onImported();
    remote.reload();
  });
  return (
    <Card className="mt-6" title="Import from Sentry" description="Browse the configured Sentry project live and import issues to investigate."
      actions={!enabled && <Button size="sm" onClick={() => setEnabled(true)}>Load Sentry issues</Button>} padded={false}>
      {!enabled ? <p className="p-5 text-sm text-ink-muted">Requires SENTRY_AUTH_TOKEN, SENTRY_ORG and SENTRY_PROJECT.</p>
        : remote.loading ? <LoadingRows rows={3} /> : remote.error ? (
          <div className="p-4"><ErrorState error={remote.error} onRetry={remote.reload} /></div>
        ) : remote.data && remote.data.items.length ? (
          <Table caption="Live Sentry issues">
            <thead><tr><Th>Issue</Th><Th>Level</Th><Th>Events</Th><Th>Last seen</Th><Th><span className="sr-only">Action</span></Th></tr></thead>
            <tbody>
              {remote.data.items.map((issue) => (
                <tr key={issue.sentry_issue_id}>
                  <Td className="max-w-md"><span className="line-clamp-2">{issue.title}</span>
                    <span className="font-mono text-xs text-ink-faint">{issue.short_id}</span></Td>
                  <Td><LevelBadge level={issue.level} /></Td>
                  <Td>{issue.event_count}</Td>
                  <Td className="text-xs text-ink-muted">{timeAgo(issue.last_seen)}</Td>
                  <Td>
                    {issue.ingested_id ? <Link className="text-xs text-accent-strong" href={`/issues/${issue.ingested_id}`}>Open</Link> : (
                      <Button size="sm" icon={<Download className="size-3.5" />} loading={importIssue.pending}
                        onClick={() => importIssue.run(issue.sentry_issue_id)}>Import</Button>
                    )}
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        ) : <EmptyState title="No unresolved issues in Sentry" />}
      {importIssue.error && <div className="p-4"><ErrorState error={importIssue.error} /></div>}
    </Card>
  );
}

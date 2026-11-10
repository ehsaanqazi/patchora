"use client";

import { Plug, Plus, RefreshCw, Trash2 } from "lucide-react";
import { useState } from "react";

import { useApp } from "@/components/AppProviders";
import {
  Badge, Button, Card, ConfirmDialog, DemoBadge, EmptyState, ErrorState, Field, InlineAlert, Input, LoadingRows,
  PageHeader, Textarea, Toggle,
} from "@/components/ui";
import { useAction, useApi } from "@/hooks/useApi";
import { api, type ConnectionTestResult, type RepositoryOut } from "@/lib/api";
import { timeAgo } from "@/lib/format";

export default function RepositoriesPage() {
  const repos = useApi(() => api.repositories(), []);
  const { isAdmin } = useApp();
  return (
    <>
      <PageHeader title="Repositories"
        description="The allowlist of repositories Patchora may read and open pull requests against, and how Sentry projects map to them." />
      {isAdmin && <ConnectRepository onConnected={repos.reload} />}
      <div className="mt-6 space-y-4">
        {repos.loading && !repos.data ? <LoadingRows /> : repos.error ? <ErrorState error={repos.error} onRetry={repos.reload} />
          : repos.data && repos.data.items.length ? repos.data.items.map((repo) => (
            <RepositoryCard key={repo.id} repo={repo} onChange={repos.reload} isAdmin={isAdmin} />
          )) : <Card><EmptyState title="No repositories connected"
            description="Connect a GitHub repository above, or seed demo data from the Overview page." /></Card>}
      </div>
    </>
  );
}

function ConnectRepository({ onConnected }: { onConnected: () => void }) {
  const [fullName, setFullName] = useState("");
  const remote = useApi(() => api.remoteRepositories().catch(() => []), []);
  const connect = useAction(async () => {
    await api.connectRepository(fullName.trim(), false);
    setFullName("");
    onConnected();
    remote.reload();
  });
  const options = (remote.data ?? []).filter((r) => !r.connected_id);
  return (
    <Card title="Connect a GitHub repository"
      description="New repositories start disabled. Enable them explicitly to add them to the allowlist.">
      <form className="flex flex-wrap items-end gap-3" onSubmit={(e) => { e.preventDefault(); void connect.run(); }}>
        <div className="min-w-[260px] flex-1">
          <Field label="Repository (owner/name)" hint={options.length ? `${options.length} accessible repositories found` : "Requires GitHub credentials"}>
            {(id) => (
              <>
                <Input id={id} list="remote-repos" placeholder="acme/web-api" value={fullName} required
                  pattern="[A-Za-z0-9-]+/[A-Za-z0-9._-]+" onChange={(e) => setFullName(e.target.value)} />
                <datalist id="remote-repos">{options.map((r) => <option key={r.full_name} value={r.full_name} />)}</datalist>
              </>
            )}
          </Field>
        </div>
        <Button type="submit" variant="primary" icon={<Plus className="size-4" />} loading={connect.pending}>Connect</Button>
      </form>
      {connect.error && <div className="mt-3"><ErrorState error={connect.error} /></div>}
    </Card>
  );
}

function RepositoryCard({ repo, onChange, isAdmin }: { repo: RepositoryOut; onChange: () => void; isAdmin: boolean }) {
  const [test, setTest] = useState<ConnectionTestResult>();
  const [confirmEnable, setConfirmEnable] = useState<boolean | null>(null);
  const [commandsText, setCommandsText] = useState(
    repo.test_commands ? repo.test_commands.map((c) => (c.argv as string[]).join(" ")).join("\n") : "");
  const toggle = useAction(async (enabled: boolean) => { await api.updateRepository(repo.id, { enabled }); setConfirmEnable(null); onChange(); });
  const testConn = useAction(async () => setTest(await api.testRepository(repo.id)));
  const sync = useAction(async () => { await api.syncRepository(repo.id); onChange(); });
  const saveCommands = useAction(async () => {
    const commands = commandsText.split("\n").map((l) => l.trim()).filter(Boolean)
      .map((line) => ({ argv: line.split(/\s+/), phase: "test" }));
    await api.updateRepository(repo.id, { test_commands: commands });
    onChange();
  });
  const removeMapping = useAction(async (id: number) => { await api.deleteMapping(id); onChange(); });

  return (
    <Card
      title={<span className="flex flex-wrap items-center gap-2 font-mono">{repo.full_name}{repo.is_demo && <DemoBadge />}
        <Badge tone={repo.enabled ? "ok" : "neutral"}>{repo.enabled ? "allowlisted" : "disabled"}</Badge></span>}
      description={`${repo.provider} · default branch ${repo.default_branch} · synced ${timeAgo(repo.last_synced_at)}`}
      actions={<>
        <Button size="sm" icon={<Plug className="size-3.5" />} loading={testConn.pending} onClick={() => testConn.run()}>Test</Button>
        {isAdmin && !repo.is_demo && <Button size="sm" icon={<RefreshCw className="size-3.5" />} loading={sync.pending} onClick={() => sync.run()}>Sync</Button>}
      </>}
    >
      <div className="grid gap-6 lg:grid-cols-2">
        <div className="space-y-4">
          <Toggle checked={repo.enabled} disabled={!isAdmin || toggle.pending} onChange={(v) => setConfirmEnable(v)}
            label="Allow Patchora to access this repository"
            description="Disabled repositories are never read or written, whatever an issue or model says." />
          {test && <InlineAlert tone={test.ok ? "ok" : "bad"}>{test.message}</InlineAlert>}
          {(testConn.error || toggle.error || sync.error) && <ErrorState error={(testConn.error ?? toggle.error ?? sync.error)!} />}
          {!repo.is_demo && (
            <Field label="Trusted validation commands (one per line, no shell)"
              hint="Optional. Only allowlisted executables (python, pytest, npm, node, ...) with plain arguments. Leave empty to auto-detect pytest / npm test.">
              {(id) => <Textarea id={id} rows={3} value={commandsText} disabled={!isAdmin}
                placeholder="python -m pytest -q" onChange={(e) => setCommandsText(e.target.value)} />}
            </Field>
          )}
          {isAdmin && !repo.is_demo && (
            <div>
              <Button size="sm" loading={saveCommands.pending} onClick={() => saveCommands.run()}>Save commands</Button>
              {saveCommands.error && <p className="mt-2 text-xs text-bad">{saveCommands.error.message}</p>}
            </div>
          )}
        </div>
        <div>
          <h3 className="mb-2 text-xs font-medium text-ink-muted">Sentry project mappings</h3>
          {repo.mappings.length ? (
            <ul className="space-y-2">
              {repo.mappings.map((m) => (
                <li key={m.id} className="flex items-start justify-between gap-2 rounded-lg border border-line bg-surface-2 p-3 text-xs">
                  <div className="space-y-0.5">
                    <p className="font-mono text-ink">{m.sentry_organization}/{m.sentry_project} → {m.branch ?? repo.default_branch}</p>
                    {m.strip_prefixes.length > 0 && <p className="text-ink-faint">strip: {m.strip_prefixes.join(", ")}</p>}
                    {m.path_prefixes.length > 0 && <p className="text-ink-faint">paths: {m.path_prefixes.join(", ")}</p>}
                  </div>
                  {isAdmin && !repo.is_demo && (
                    <Button size="sm" variant="ghost" aria-label="Delete mapping" icon={<Trash2 className="size-3.5" />}
                      onClick={() => removeMapping.run(m.id)} />
                  )}
                </li>
              ))}
            </ul>
          ) : <p className="text-sm text-ink-muted">No mappings. Issues from unmapped projects stop for human review.</p>}
          {isAdmin && !repo.is_demo && <AddMapping repoId={repo.id} onAdded={onChange} />}
        </div>
      </div>
      <ConfirmDialog open={confirmEnable !== null} title={confirmEnable ? "Allowlist repository" : "Remove from allowlist"}
        confirmLabel={confirmEnable ? "Enable" : "Disable"} tone={confirmEnable ? "primary" : "danger"} pending={toggle.pending}
        onCancel={() => setConfirmEnable(null)} onConfirm={() => confirmEnable !== null && toggle.run(confirmEnable)}>
        {confirmEnable
          ? <p>Patchora will be able to read <span className="font-mono text-ink">{repo.full_name}</span> and, depending on the autonomy mode, create <span className="font-mono">patchora/</span> branches and draft pull requests. It never pushes to the default branch or merges.</p>
          : <p>Runs for this repository will stop before reading code or publishing anything.</p>}
      </ConfirmDialog>
    </Card>
  );
}

function AddMapping({ repoId, onAdded }: { repoId: number; onAdded: () => void }) {
  const [org, setOrg] = useState("");
  const [project, setProject] = useState("");
  const [branch, setBranch] = useState("");
  const [strip, setStrip] = useState("");
  const add = useAction(async () => {
    await api.addMapping(repoId, {
      sentry_organization: org.trim(), sentry_project: project.trim(), branch: branch.trim() || null,
      strip_prefixes: strip.split(",").map((s) => s.trim()).filter(Boolean), path_prefixes: [],
    });
    setOrg(""); setProject(""); setBranch(""); setStrip("");
    onAdded();
  });
  return (
    <form className="mt-4 grid grid-cols-2 gap-2" onSubmit={(e) => { e.preventDefault(); void add.run(); }}>
      <Input aria-label="Sentry organization" placeholder="sentry org" value={org} required onChange={(e) => setOrg(e.target.value)} />
      <Input aria-label="Sentry project" placeholder="sentry project" value={project} required onChange={(e) => setProject(e.target.value)} />
      <Input aria-label="Branch" placeholder="branch (default)" value={branch} onChange={(e) => setBranch(e.target.value)} />
      <Input aria-label="Strip prefixes" placeholder="strip prefixes, e.g. /app/" value={strip} onChange={(e) => setStrip(e.target.value)} />
      <div className="col-span-2 flex items-center justify-between gap-2">
        {add.error ? <p className="text-xs text-bad">{add.error.message}</p> : <span />}
        <Button size="sm" type="submit" loading={add.pending} icon={<Plus className="size-3.5" />}>Add mapping</Button>
      </div>
    </form>
  );
}

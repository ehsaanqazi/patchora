"use client";

import clsx from "clsx";
import { Save, Trash2 } from "lucide-react";
import Link from "next/link";
import { useMemo, useState } from "react";

import { useApp } from "@/components/AppProviders";
import {
  Badge, Button, Card, ConfirmDialog, EmptyState, ErrorState, Field, InlineAlert, Input, LoadingRows, PageHeader,
  Pagination, Select, Table, Td, Textarea, Th, Toggle,
} from "@/components/ui";
import { useAction, useApi } from "@/hooks/useApi";
import { api, type Json } from "@/lib/api";
import { formatDate, humanize } from "@/lib/format";

const MODES = [
  { value: "analyze_only", title: "A · Analyze only",
    detail: "Retrieve the issue, investigate code, produce a diagnosis and a patch preview. Executes nothing and never writes to GitHub." },
  { value: "prepare_patch", title: "B · Prepare patch (default)",
    detail: "Also applies the patch in a disposable sandbox and runs tests, then waits for explicit reviewer approval before creating a branch and draft PR." },
  { value: "auto_pr", title: "C · Automatic PR after policy checks",
    detail: "Publishes a branch and draft PR without a reviewer only when tests reproduce and fix the failure, risk is low and AUTO_CREATE_PULL_REQUESTS=true. Never merges or deploys." },
];

export default function SettingsPage() {
  const settings = useApi(() => api.settings(), []);
  const { isAdmin, refreshDiagnostics } = useApp();
  const [edits, setEdits] = useState<Json>({});
  const [confirm, setConfirm] = useState(false);
  const draft = useMemo(() => ({ ...((settings.data?.values ?? {}) as Json), ...edits }), [settings.data, edits]);
  const changes = useMemo(() => {
    const current = (settings.data?.values ?? {}) as Json;
    return Object.fromEntries(Object.entries(edits).filter(([k, v]) => JSON.stringify(v) !== JSON.stringify(current[k])));
  }, [edits, settings.data]);
  const sensitive = Object.keys(changes).filter((k) => settings.data?.security_sensitive.includes(k));
  const save = useAction(async () => {
    await api.updateSettings(changes);
    setConfirm(false);
    setEdits({});
    settings.reload();
    refreshDiagnostics();
  });

  if (settings.loading && !settings.data) return <LoadingRows rows={8} />;
  if (settings.error || !settings.data) return <ErrorState error={settings.error ?? new Error("Unavailable")} onRetry={settings.reload} />;
  const env = settings.data.environment as Json;
  const set = (key: string, value: unknown) => setEdits({ ...edits, [key]: value });
  const num = (key: string) => Number(draft[key] ?? 0);

  return (
    <>
      <PageHeader title="Settings" description="Autonomy, limits and safety policy. Security-relevant changes are recorded in the audit log."
        actions={isAdmin && <Button variant="primary" icon={<Save className="size-4" />} disabled={!Object.keys(changes).length}
          onClick={() => (sensitive.length ? setConfirm(true) : save.run())} loading={save.pending}>
          Save {Object.keys(changes).length ? `(${Object.keys(changes).length})` : ""}</Button>} />
      {!isAdmin && <div className="mb-4"><InlineAlert tone="warn">Only administrators can change settings.</InlineAlert></div>}
      {save.error && <div className="mb-4"><ErrorState error={save.error} /></div>}

      <fieldset disabled={!isAdmin} className="space-y-6">
        <Card title="Autonomy mode" description="What the agent may do without a human. Applies to new runs.">
          <div role="radiogroup" aria-label="Autonomy mode" className="grid gap-3 md:grid-cols-3">
            {MODES.map((mode) => (
              <button key={mode.value} type="button" role="radio" aria-checked={draft.autonomy_mode === mode.value}
                onClick={() => set("autonomy_mode", mode.value)}
                className={clsx("rounded-lg border p-4 text-left transition-colors",
                  draft.autonomy_mode === mode.value ? "border-accent bg-accent-soft" : "border-line bg-surface-2 hover:border-line-strong")}>
                <p className="text-sm font-medium text-ink">{mode.title}</p>
                <p className="mt-1 text-xs text-ink-muted">{mode.detail}</p>
              </button>
            ))}
          </div>
          <p className="mt-3 text-xs text-ink-faint">
            AUTO_CREATE_PULL_REQUESTS (environment): <Badge tone={env.auto_create_pull_requests_env ? "warn" : "neutral"}>{String(env.auto_create_pull_requests_env)}</Badge>
          </p>
        </Card>

        <div className="grid gap-6 lg:grid-cols-2">
          <Card title="Limits">
            <div className="grid grid-cols-2 gap-4">
              <Field label="Max repair attempts" hint="After the first attempt (0–5)">
                {(id) => <Input id={id} type="number" min={0} max={5} value={num("max_agent_retries")} onChange={(e) => set("max_agent_retries", Number(e.target.value))} />}
              </Field>
              <Field label="Test timeout (seconds)">
                {(id) => <Input id={id} type="number" min={10} max={3600} value={num("max_test_runtime_seconds")} onChange={(e) => set("max_test_runtime_seconds", Number(e.target.value))} />}
              </Field>
              <Field label="Max files per patch">
                {(id) => <Input id={id} type="number" min={1} max={50} value={num("max_patch_files")} onChange={(e) => set("max_patch_files", Number(e.target.value))} />}
              </Field>
              <Field label="Max changed lines">
                {(id) => <Input id={id} type="number" min={1} max={5000} value={num("max_patch_lines")} onChange={(e) => set("max_patch_lines", Number(e.target.value))} />}
              </Field>
              <Field label="Audit retention (days)">
                {(id) => <Input id={id} type="number" min={7} value={num("audit_retention_days")} onChange={(e) => set("audit_retention_days", Number(e.target.value))} />}
              </Field>
              <Field label="Run retention (days)">
                {(id) => <Input id={id} type="number" min={7} value={num("run_retention_days")} onChange={(e) => set("run_retention_days", Number(e.target.value))} />}
              </Field>
            </div>
          </Card>

          <Card title="Sandbox" description="Where repository tests run. Docker is the isolation boundary.">
            <div className="space-y-4">
              <Field label="Execution backend">
                {(id) => (
                  <Select id={id} value={String(draft.sandbox_backend)} onChange={(e) => set("sandbox_backend", e.target.value)}>
                    <option value="docker">Docker (isolated, recommended)</option>
                    <option value="local">Local subprocess (development only, weak isolation)</option>
                    <option value="disabled">Disabled (analysis and patch preview only)</option>
                  </Select>
                )}
              </Field>
              {draft.sandbox_backend === "local" && (
                <>
                  <InlineAlert tone="warn">The local runner is not a security boundary. Use it only with repositories you trust, in development.</InlineAlert>
                  <Toggle checked={Boolean(draft.allow_unsafe_local_execution)} onChange={(v) => set("allow_unsafe_local_execution", v)}
                    label="Allow unsafe local execution" description="Required for the local runner; refused outside development." />
                </>
              )}
              <Toggle checked={Boolean(draft.sandbox_allow_dependency_install)} onChange={(v) => set("sandbox_allow_dependency_install", v)}
                label="Install dependencies (setup phase)" description="Networked setup step in its own container, binary wheels only, no secrets." />
            </div>
          </Card>
        </div>

        <Card title="Protected paths" description="Glob patterns the agent may never modify (CI, deployment, secrets, dependency manifests, permissions).">
          <Textarea aria-label="Protected paths" rows={8} value={((draft.protected_paths as string[]) ?? []).join("\n")}
            onChange={(e) => set("protected_paths", e.target.value.split("\n").map((l) => l.trim()).filter(Boolean))} />
        </Card>

        <div className="grid gap-6 lg:grid-cols-2">
          <Card title="Model & retrieval">
            <div className="space-y-4">
              <p className="text-sm text-ink-muted">
                The Groq API key and model are configured on the <Link href="/integrations" className="text-accent-strong hover:underline">Integrations</Link> page.
              </p>
              <Toggle checked={Boolean(draft.faiss_enabled)} onChange={(v) => set("faiss_enabled", v)}
                label="Semantic code search (FAISS)" description={`Optional; embeddings computed locally (${String(env.embedding_model ?? "default model")}). Never used for authorization.`} />
            </div>
          </Card>
          <Card title="Workflow">
            <div className="space-y-4">
              <Toggle checked={Boolean(draft.pr_draft)} onChange={(v) => set("pr_draft", v)} label="Open pull requests as drafts" />
              <Toggle checked={Boolean(draft.webhook_auto_analyze)} onChange={(v) => set("webhook_auto_analyze", v)}
                label="Analyze new issues from webhooks" description="Only for projects with a repository mapping." />
              <Toggle checked={Boolean(draft.create_issue_on_human_review)} onChange={(v) => set("create_issue_on_human_review", v)}
                label="Create a GitHub issue when a run needs human review" />
            </div>
          </Card>
        </div>
      </fieldset>

      <AuditLog />
      {isAdmin && <DemoReset />}

      <ConfirmDialog open={confirm} title="Change security-relevant settings?" confirmLabel="Save changes" pending={save.pending}
        onCancel={() => setConfirm(false)} onConfirm={() => save.run()}>
        <p>These changes alter what the agent is allowed to do and will be recorded in the audit log:</p>
        <ul className="list-disc pl-5 font-mono text-xs text-ink">{sensitive.map((k) => <li key={k}>{k}</li>)}</ul>
      </ConfirmDialog>
    </>
  );
}

function AuditLog() {
  const [offset, setOffset] = useState(0);
  const audit = useApi(() => api.audit({ limit: 15, offset }), [offset]);
  return (
    <Card className="mt-6" title="Audit log" description="Append-only record of consequential actions (approvals, publication, settings, webhooks)." padded={false}>
      {audit.loading && !audit.data ? <LoadingRows /> : audit.error ? <div className="p-4"><ErrorState error={audit.error} /></div>
        : audit.data?.items.length ? (
          <>
            <Table caption="Audit log">
              <thead><tr><Th>When</Th><Th>Actor</Th><Th>Action</Th><Th>Resource</Th><Th>Outcome</Th></tr></thead>
              <tbody>
                {audit.data.items.map((e) => (
                  <tr key={e.id}>
                    <Td className="whitespace-nowrap text-xs text-ink-muted">{formatDate(e.created_at)}</Td>
                    <Td className="text-xs">{e.actor_type}{e.actor_id ? `:${e.actor_id}` : ""}</Td>
                    <Td className="font-mono text-xs">{e.action}</Td>
                    <Td className="text-xs text-ink-muted">{e.resource_type}{e.resource_id ? ` #${e.resource_id}` : ""}</Td>
                    <Td><Badge tone={e.outcome === "success" ? "ok" : e.outcome === "failure" ? "bad" : "neutral"}>{humanize(e.outcome)}</Badge></Td>
                  </tr>
                ))}
              </tbody>
            </Table>
            <Pagination total={audit.data.total} limit={15} offset={offset} onChange={setOffset} />
          </>
        ) : <EmptyState title="No audit entries" />}
    </Card>
  );
}

function DemoReset() {
  const [open, setOpen] = useState(false);
  const [result, setResult] = useState<Record<string, number>>();
  const reset = useAction(async () => { setResult(await api.resetDemo()); setOpen(false); });
  return (
    <Card className="mt-6" title="Demo data" description="Removes only rows flagged as demo data. Real repositories, runs and configuration are never touched.">
      <Button variant="danger" icon={<Trash2 className="size-4" />} onClick={() => setOpen(true)}>Reset demo data</Button>
      {result && <p className="mt-3 text-xs text-ink-muted">Removed: {Object.entries(result).map(([k, v]) => `${v} ${k}`).join(", ")}</p>}
      {reset.error && <div className="mt-3"><ErrorState error={reset.error} /></div>}
      <ConfirmDialog open={open} title="Reset demo data?" confirmLabel="Reset" tone="danger" pending={reset.pending}
        onCancel={() => setOpen(false)} onConfirm={() => reset.run()}>
        <p>Demo issues, runs, patches, approvals and simulated pull requests will be deleted. Audit entries are kept.</p>
      </ConfirmDialog>
    </Card>
  );
}

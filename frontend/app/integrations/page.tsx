"use client";

import { BrainCircuit, Eraser, KeyRound, Plug, Save } from "lucide-react";
import { useState } from "react";

import { useApp } from "@/components/AppProviders";
import {
  Badge, Button, Card, ConfirmDialog, ErrorState, Field, InlineAlert, Input, LoadingRows, PageHeader, Select, Textarea,
} from "@/components/ui";
import { useAction, useApi } from "@/hooks/useApi";
import { api, type ConnectionTestResult, type IntegrationOut } from "@/lib/api";
import { formatDate } from "@/lib/format";

type FieldOut = IntegrationOut["fields"][number];

const DESCRIPTIONS: Record<string, string> = {
  groq: "Fast hosted open models through Groq.",
  anthropic: "Claude models through the Anthropic API.",
  openai: "OpenAI models (or any OpenAI-compatible endpoint via Base URL).",
  deepseek: "DeepSeek models through the DeepSeek API.",
  gemini: "Google Gemini models through Gemini's OpenAI-compatible endpoint.",
  bedrock: "Any Bedrock chat model (Claude, Llama, Mistral, Nova…) via the Converse API. Uses the server's AWS credentials when no key is saved.",
  sentry: "Read-only access to issues, events and release commits, plus signed webhooks.",
  github: "Repository contents, patchora/ branches, issues and pull requests. Use a GitHub App or a personal access token.",
};

const SOURCE_LABEL: Record<string, string> = { saved: "saved", env: "from environment", default: "default" };

export default function IntegrationsPage() {
  const cards = useApi(() => api.integrations(), []);
  const llmCards = cards.data?.filter((c) => c.category === "llm") ?? [];
  const otherCards = cards.data?.filter((c) => c.category !== "llm") ?? [];
  return (
    <>
      <PageHeader title="Integrations"
        description="Configure every integration here. Values are stored in the Patchora database and used immediately — no environment file or restart needed." />
      {cards.data?.[0]?.notes[0] && <div className="mb-4"><InlineAlert tone="warn">{cards.data[0].notes[0]}</InlineAlert></div>}
      {cards.loading && !cards.data ? <LoadingRows /> : cards.error ? <ErrorState error={cards.error} onRetry={cards.reload} /> : (
        <div className="space-y-8">
          <div className="grid gap-6 xl:grid-cols-2">
            {otherCards.map((card) => <IntegrationCard key={card.provider} card={card} onChange={cards.reload} />)}
          </div>
          <section aria-labelledby="llm-heading" className="space-y-4">
            <div>
              <h2 id="llm-heading" className="text-base font-semibold text-ink">Model providers</h2>
              <p className="mt-1 text-sm text-ink-muted">
                The agent uses one provider for diagnosis, patch planning and patch generation. Configure any of them,
                then choose the active one.
              </p>
            </div>
            <ActiveProvider cards={llmCards} onChange={cards.reload} />
            <div className="grid gap-6 xl:grid-cols-3">
              {llmCards.map((card) => <IntegrationCard key={card.provider} card={card} onChange={cards.reload} />)}
            </div>
          </section>
        </div>
      )}
    </>
  );
}

function ActiveProvider({ cards, onChange }: { cards: IntegrationOut[]; onChange: () => void }) {
  const { isAdmin, refreshDiagnostics } = useApp();
  const active = cards.find((c) => c.active);
  const [choice, setChoice] = useState<string>();
  const [confirm, setConfirm] = useState(false);
  const selected = choice ?? active?.provider ?? "";
  const target = cards.find((c) => c.provider === selected);
  const save = useAction(async () => {
    await api.updateSettings({ llm_provider: selected });
    setConfirm(false);
    setChoice(undefined);
    onChange();
    refreshDiagnostics();
  });
  return (
    <Card title={<span className="flex items-center gap-2"><BrainCircuit className="size-4 text-accent-strong" aria-hidden />Active model provider</span>}
      description="Every new run uses this provider. Changing it is recorded in the audit log, because it decides which vendor receives your code and error data.">
      <div className="flex flex-wrap items-end gap-3">
        <div className="min-w-56 flex-1">
          <Field label="Provider">
            {(id) => (
              <Select id={id} value={selected} disabled={!isAdmin || save.pending} onChange={(e) => setChoice(e.target.value)}>
                {cards.map((c) => (
                  <option key={c.provider} value={c.provider}>
                    {c.label || c.provider}{c.configured ? "" : " (not configured)"}
                  </option>
                ))}
              </Select>
            )}
          </Field>
        </div>
        {isAdmin && (
          <Button variant="primary" disabled={!choice || choice === active?.provider} loading={save.pending}
            onClick={() => setConfirm(true)}>Use this provider</Button>
        )}
      </div>
      {active && !active.configured && (
        <div className="mt-3"><InlineAlert tone="warn">{active.label} is active but not configured yet — runs will stop until you add its key and model below.</InlineAlert></div>
      )}
      {save.error && <div className="mt-3"><ErrorState error={save.error} /></div>}
      <ConfirmDialog open={confirm} title={`Switch to ${target?.label ?? selected}?`} confirmLabel="Switch provider"
        pending={save.pending} onCancel={() => setConfirm(false)} onConfirm={() => save.run()}>
        <p>New runs will send repository code and error data to {target?.label ?? selected}. Runs already in progress keep their current provider.</p>
        {target && !target.configured && <p className="mt-2 text-warn">{target.label} is not configured yet.</p>}
      </ConfirmDialog>
    </Card>
  );
}

function IntegrationCard({ card, onChange }: { card: IntegrationOut; onChange: () => void }) {
  const { isAdmin, refreshDiagnostics } = useApp();
  const [result, setResult] = useState<ConnectionTestResult>();
  const [values, setValues] = useState<Record<string, string>>({});
  const [clearing, setClearing] = useState<FieldOut | null>(null);
  const [saved, setSaved] = useState(false);

  const test = useAction(async () => { setResult(await api.testIntegration(card.provider)); onChange(); refreshDiagnostics(); });
  const save = useAction(async () => {
    // Only fields the user edited are sent; untouched secret fields keep their stored value.
    const payload = Object.fromEntries(Object.entries(values).filter(([, v]) => v.trim() !== ""));
    await api.saveCredentials(card.provider, payload);
    setValues({});
    setSaved(true);
    onChange();
    refreshDiagnostics();
  });
  const clear = useAction(async (name: string) => {
    await api.saveCredentials(card.provider, { [name]: null });
    setClearing(null);
    onChange();
    refreshDiagnostics();
  });
  const dirty = Object.values(values).some((v) => v.trim() !== "");
  const models = (result?.details?.available_models as string[] | undefined) ?? [];

  return (
    <Card
      title={<span className="flex items-center gap-2"><Plug className="size-4 text-accent-strong" aria-hidden />{card.label || card.provider}</span>}
      description={DESCRIPTIONS[card.provider]}
      actions={<span className="flex flex-wrap justify-end gap-1.5">
        {card.active && <Badge tone="accent">active</Badge>}
        <Badge tone={card.configured ? "ok" : "neutral"}>{card.configured ? (card.mode?.replace(/_/g, " ") ?? "configured") : "not configured"}</Badge>
      </span>}
    >
      <form className="space-y-4" autoComplete="off" onSubmit={(e) => { e.preventDefault(); setSaved(false); void save.run(); }}>
        {card.fields.map((field) => (
          <IntegrationField key={field.name} field={field} disabled={!isAdmin} value={values[field.name] ?? ""}
            onChange={(v) => { setSaved(false); setValues({ ...values, [field.name]: v }); }}
            onClear={() => setClearing(field)} />
        ))}
        {save.error && <ErrorState error={save.error} />}
        {saved && !dirty && <InlineAlert tone="ok">Saved to the database.</InlineAlert>}
        <div className="flex flex-wrap items-center gap-2 border-t border-line pt-4">
          {isAdmin && <Button type="submit" variant="primary" size="sm" icon={<Save className="size-3.5" />} disabled={!dirty} loading={save.pending}>Save</Button>}
          <Button size="sm" icon={<Plug className="size-3.5" />} loading={test.pending} onClick={() => test.run()}>Test connection</Button>
        </div>
      </form>

      <p className="mt-3 text-xs text-ink-faint">
        {card.last_tested_at ? `Last test ${formatDate(card.last_tested_at)}: ${card.last_ok ? "ok" : "failed"}` : "Never tested"}
        {card.last_success_at && !card.last_ok ? ` · last success ${formatDate(card.last_success_at)}` : ""}
      </p>
      {result && <div className="mt-3"><InlineAlert tone={result.ok ? "ok" : "bad"}>{result.message}</InlineAlert></div>}
      {!result && card.last_message && card.last_ok === false && <p className="mt-2 text-xs text-bad">{card.last_message}</p>}
      {test.error && <div className="mt-3"><ErrorState error={test.error} /></div>}
      {models.length > 0 && (
        <details className="mt-3 text-xs" open>
          <summary className="cursor-pointer text-accent-strong">{models.length} models available to this key — click one to use it</summary>
          <ul className="mt-2 max-h-48 space-y-0.5 overflow-auto font-mono">
            {models.map((m) => (
              <li key={m}>
                <button type="button" disabled={!isAdmin} className="text-ink-muted hover:text-accent-strong disabled:hover:text-ink-muted"
                  onClick={() => { setSaved(false); setValues({ ...values, model: m }); }}>{m}</button>
              </li>
            ))}
          </ul>
        </details>
      )}

      <ConfirmDialog open={clearing !== null} title={`Clear ${clearing?.label ?? ""}?`} confirmLabel="Clear" tone="danger"
        pending={clear.pending} onCancel={() => setClearing(null)} onConfirm={() => clearing && clear.run(clearing.name)}>
        <p>The saved value is deleted from the database.{clearing?.source === "saved" ? "" : " (Nothing is saved for this field.)"}</p>
        {clear.error && <ErrorState error={clear.error} />}
      </ConfirmDialog>
    </Card>
  );
}

function IntegrationField({ field, value, disabled, onChange, onClear }: {
  field: FieldOut; value: string; disabled: boolean; onChange: (value: string) => void; onClear: () => void;
}) {
  const status = field.configured
    ? `${field.secret ? "set" : "configured"}${field.source ? ` · ${SOURCE_LABEL[field.source] ?? field.source}` : ""}`
    : field.required ? "required" : "optional";
  const placeholder = field.secret
    ? (field.configured ? "•••••••• stored — type to replace" : "not set")
    : (field.value ?? "not set");
  return (
    <Field label={field.label || field.name} hint={field.help || undefined}>
      {(id) => (
        <div className="space-y-1.5">
          <div className="flex items-center justify-between gap-2">
            <Badge tone={field.configured ? "ok" : field.required ? "warn" : "neutral"}>{status}</Badge>
            {field.source === "saved" && !disabled && (
              <button type="button" onClick={onClear} className="inline-flex items-center gap-1 text-xs text-ink-faint hover:text-bad">
                <Eraser className="size-3" aria-hidden />Clear
              </button>
            )}
          </div>
          {field.kind === "pem" ? (
            <Textarea id={id} rows={4} spellCheck={false} disabled={disabled} placeholder={placeholder}
              value={value} onChange={(e) => onChange(e.target.value)} />
          ) : (
            <div className="relative">
              {field.secret && <KeyRound className="pointer-events-none absolute left-3 top-2.5 size-4 text-ink-faint" aria-hidden />}
              <Input id={id} type={field.secret ? "password" : "text"} autoComplete="off" spellCheck={false}
                disabled={disabled} placeholder={placeholder} value={value} className={field.secret ? "pl-9" : undefined}
                onChange={(e) => onChange(e.target.value)} />
            </div>
          )}
        </div>
      )}
    </Field>
  );
}

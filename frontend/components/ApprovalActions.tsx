"use client";

import { Check, X } from "lucide-react";
import { useState } from "react";

import { useApp } from "@/components/AppProviders";
import { Button, ConfirmDialog, ErrorState, Field, Textarea } from "@/components/ui";
import { useAction } from "@/hooks/useApi";
import { api, type ApprovalOut } from "@/lib/api";

/** Approve or reject a pending publication approval. Both require explicit confirmation. */
export function ApprovalActions({ approval, onDone, isDemo }: {
  approval: Pick<ApprovalOut, "id" | "status" | "files_changed" | "risk_summary">; onDone: () => void; isDemo: boolean;
}) {
  const { canReview } = useApp();
  const [dialog, setDialog] = useState<"approve" | "reject" | null>(null);
  const [reason, setReason] = useState("");
  const act = useAction(async (kind: "approve" | "reject") => {
    if (kind === "approve") await api.approve(approval.id, reason);
    else await api.reject(approval.id, reason);
    setDialog(null);
    setReason("");
    onDone();
  });
  if (approval.status !== "pending") return null;
  if (!canReview) return <p className="text-xs text-ink-faint">Reviewer role required to resolve approvals.</p>;
  const reasons = (approval.risk_summary?.reasons as string[] | undefined) ?? [];
  return (
    <>
      <div className="flex flex-wrap gap-2">
        <Button variant="primary" icon={<Check className="size-4" />} onClick={() => setDialog("approve")}>Approve & publish</Button>
        <Button variant="danger" icon={<X className="size-4" />} onClick={() => setDialog("reject")}>Reject</Button>
      </div>
      <ConfirmDialog
        open={dialog !== null}
        title={dialog === "approve" ? "Approve publication" : "Reject patch"}
        confirmLabel={dialog === "approve" ? "Approve" : "Reject"}
        tone={dialog === "approve" ? "primary" : "danger"}
        pending={act.pending}
        onCancel={() => { setDialog(null); act.clearError(); }}
        onConfirm={() => dialog && act.run(dialog)}
      >
        {dialog === "approve" ? (
          <>
            <p>
              Patchora will re-run every policy check, then create a new <span className="font-mono">patchora/</span> branch
              and open a {isDemo ? "simulated" : "draft"} pull request. It never pushes to the default branch, merges or deploys.
            </p>
            <p>Files: <span className="font-mono text-ink">{approval.files_changed.join(", ")}</span></p>
            {reasons.length > 0 && <ul className="list-disc pl-5 text-xs">{reasons.map((r) => <li key={r}>{r}</li>)}</ul>}
          </>
        ) : <p>The run will be closed as rejected. Nothing will be published.</p>}
        <Field label="Reason (recorded in the audit log)">
          {(id) => <Textarea id={id} rows={3} value={reason} maxLength={2000} onChange={(e) => setReason(e.target.value)} />}
        </Field>
        {act.error && <ErrorState error={act.error} />}
      </ConfirmDialog>
    </>
  );
}

/**
 * In-browser mock of the FastAPI backend, used when NEXT_PUBLIC_DEMO_MODE=true.
 * State lives in memory, so changes (approvals, settings, ...) persist until the page is reloaded.
 */
import { ApiError, type Page, type RunDetail } from "@/lib/api";

import * as data from "./data";

type Query = Record<string, string | number | boolean | null | undefined>;

const state = {
  runs: structuredClone(data.runs),
  approvals: structuredClone(data.approvals),
  repositories: structuredClone(data.repositories),
  settings: structuredClone(data.settings),
  issues: structuredClone(data.issues),
};

const delay = (ms = 150) => new Promise((resolve) => setTimeout(resolve, ms));

function page<T>(items: T[], query?: Query): Page<T> {
  const limit = Number(query?.limit ?? 25);
  const offset = Number(query?.offset ?? 0);
  return { items: items.slice(offset, offset + limit), total: items.length, limit, offset };
}

function notFound(what: string): never {
  throw new ApiError(`${what} not found`, 404, "not_found");
}

function findRun(id: number): RunDetail {
  return state.runs.find((r) => r.id === id) ?? notFound(`Run ${id}`);
}

const ok = (provider: string) => ({ provider, ok: true, message: "Demo mode: simulated connection check succeeded.", details: {} });

export async function demoRequest<T>(method: string, path: string, body?: unknown, query?: Query): Promise<T> {
  await delay();
  return route(method, path, (body ?? {}) as Record<string, unknown>, query) as T;
}

function route(method: string, path: string, body: Record<string, unknown>, query?: Query): unknown {
  const seg = path.split("/").filter(Boolean);
  const id = Number(seg.find((s) => /^\d+$/.test(s)));
  const key = `${method} /${seg.map((s) => (/^\d+$/.test(s) ? ":id" : s)).join("/")}`;

  switch (key) {
    // health & auth
    case "GET /diagnostics": return data.diagnostics;
    case "GET /auth/me": return data.me;
    case "POST /auth/login": return { display_name: data.me.display_name, role: data.me.role };
    case "POST /auth/logout": return { ok: true };

    // dashboard
    case "GET /dashboard/summary": {
      const runs = state.runs;
      const count = (pred: (r: RunDetail) => boolean) => runs.filter(pred).length;
      const outcomes: Record<string, number> = {};
      for (const r of runs) if (r.outcome) outcomes[r.outcome] = (outcomes[r.outcome] ?? 0) + 1;
      return {
        demo: true, issues_ingested: state.issues.length,
        issues_investigated: new Set(runs.map((r) => r.sentry_issue_id)).size, runs_total: runs.length,
        runs_active: count((r) => !["COMPLETED", "FAILED", "CANCELLED", "NEEDS_HUMAN_REVIEW", "WAITING_FOR_APPROVAL"].includes(r.status)),
        runs_failed: count((r) => r.status === "FAILED"), runs_needing_review: count((r) => r.status === "NEEDS_HUMAN_REVIEW"),
        successful_validations: count((r) => r.validation?.outcome === "passed"),
        awaiting_approval: state.approvals.filter((a) => a.status === "pending").length,
        open_pull_requests: data.pullRequests.filter((p) => p.status === "open").length,
        abstentions: count((r) => !!r.outcome?.startsWith("abstained")), outcomes,
      };
    }
    case "GET /dashboard/activity": return data.activity.slice(0, Number(query?.limit ?? 15));

    // integrations
    case "GET /integrations": return data.integrations;
    case "POST /integrations/groq/test":
    case "POST /integrations/sentry/test":
    case "POST /integrations/github/test": return ok(seg[1] ?? "");
    case "PUT /integrations/groq/credentials":
    case "PUT /integrations/sentry/credentials":
    case "PUT /integrations/github/credentials":
      throw new ApiError("Credentials cannot be saved in the static demo.", 403, "demo_read_only");
    case "GET /integrations/groq/models":
      return { models: [{ id: data.diagnostics.groq.model, active: true, context_window: 131072 }] };

    // sentry issues
    case "GET /sentry/issues": {
      let items = state.issues;
      const q = String(query?.q ?? "").toLowerCase();
      if (q) items = items.filter((i) => `${i.title} ${i.short_id} ${i.culprit}`.toLowerCase().includes(q));
      if (query?.level) items = items.filter((i) => i.level === query.level);
      if (query?.status) items = items.filter((i) => i.status === query.status);
      return page(items, query);
    }
    case "GET /sentry/issues/:id":
      return data.issueDetail(id, state.runs.map(data.toRunOut)) ?? notFound(`Issue ${id}`);
    case "GET /sentry/remote-issues": return { items: [], next_cursor: null };
    case "POST /sentry/issues/import":
      throw new ApiError("Importing from Sentry is not available in the static demo.", 403, "demo_read_only");
    case "POST /sentry/issues/:id/analyze": {
      const latest = state.runs.find((r) => r.sentry_issue_id === id);
      return latest ? data.toRunOut(latest) : notFound(`A demo run for issue ${id}`);
    }
    case "POST /sentry/issues/:id/repository":
      return state.issues.find((i) => i.id === id) ?? notFound(`Issue ${id}`);

    // repositories
    case "GET /repositories": return page(state.repositories, query);
    case "GET /repositories/remote":
      return state.repositories.map((r) => ({ full_name: r.full_name, default_branch: r.default_branch, private: r.private, connected_id: r.id }));
    case "POST /repositories":
      throw new ApiError("Connecting repositories is not available in the static demo.", 403, "demo_read_only");
    case "PATCH /repositories/:id": {
      const repo = state.repositories.find((r) => r.id === id) ?? notFound(`Repository ${id}`);
      if (typeof body.enabled === "boolean") repo.enabled = body.enabled;
      if (Array.isArray(body.test_commands)) repo.test_commands = body.test_commands as Record<string, unknown>[];
      return repo;
    }
    case "POST /repositories/:id/test-connection": return ok("github");
    case "POST /repositories/:id/sync": return state.repositories.find((r) => r.id === id) ?? notFound(`Repository ${id}`);
    case "POST /repositories/:id/mappings":
    case "DELETE /repositories/mappings/:id":
      throw new ApiError("Editing mappings is not available in the static demo.", 403, "demo_read_only");

    // runs
    case "GET /runs": {
      let items = state.runs;
      if (query?.status) items = items.filter((r) => r.status === query.status);
      return page(items.map(data.toRunOut), query);
    }
    case "GET /runs/:id": return findRun(id);
    case "GET /runs/:id/diff": return data.diffs[id] ?? "";
    case "POST /runs/:id/retry":
    case "POST /runs/:id/cancel": return data.toRunOut(findRun(id));
    case "POST /runs/:id/validate": {
      const p = findRun(id).patches.at(-1) ?? notFound("Patch");
      return { blocked: p.policy_status === "failed", findings: p.policy_findings, files: p.files_changed,
        lines_added: p.lines_added, lines_removed: p.lines_removed, risk_level: p.risk_level ?? "unknown" };
    }
    case "POST /runs/:id/github-issue": return { created: true, url: "demo://issues/7", number: 7 };
    case "POST /runs/:id/pull-request": return { status: "simulated", url: "demo://pull/43" };

    // approvals & pull requests
    case "GET /approvals": {
      const items = query?.status ? state.approvals.filter((a) => a.status === query.status) : state.approvals;
      return page(items, query);
    }
    case "POST /approvals/:id/approve":
    case "POST /approvals/:id/reject": {
      const approval = state.approvals.find((a) => a.id === id) ?? notFound(`Approval ${id}`);
      const approved = seg[2] === "approve";
      Object.assign(approval, { status: approved ? "approved" : "rejected", reviewer_id: data.me.id,
        reviewer_name: data.me.display_name, reason: body.reason ?? null, resolved_at: new Date().toISOString(),
        run_status: approved ? "COMPLETED" : "COMPLETED" });
      const run = state.runs.find((r) => r.id === approval.run_id);
      if (run) {
        Object.assign(run, { status: "COMPLETED", current_step: "COMPLETED", outcome: approved ? "patch_validated" : "rejected",
          completed_at: new Date().toISOString() });
        run.approvals = [approval as unknown as Record<string, unknown>];
        const last = run.steps.at(-1);
        if (last?.status === "running") Object.assign(last, { status: "succeeded", completed_at: new Date().toISOString(),
          output_summary: approved ? "Approved by Demo Admin (publication is simulated in the demo)." : "Rejected by Demo Admin." });
      }
      return approval;
    }
    case "GET /pull-requests": {
      const items = query?.status ? data.pullRequests.filter((p) => p.status === query.status) : data.pullRequests;
      return page(items, query);
    }
    case "GET /pull-requests/:id": return data.pullRequests.find((p) => p.id === id) ?? notFound(`Pull request ${id}`);

    // settings & audit
    case "GET /settings": return state.settings;
    case "PUT /settings":
      state.settings = { ...state.settings, values: { ...state.settings.values, ...(body.values as object) } };
      return state.settings;
    case "GET /audit": return page(data.audit, query);

    // demo
    case "GET /demo/scenarios": return data.demoScenarios;
    case "POST /demo/seed": return { seeded: true };
    case "POST /demo/runs": return { run_id: data.scenarioRun[String(body.scenario)] ?? 101, created: false };
    case "POST /demo/reset": return { runs: 0, issues: 0, repositories: 0 };

    // evaluations
    case "GET /evaluations": return page(data.evaluations.map((e) => ({ ...e, results: undefined })));
    case "GET /evaluations/:id": return data.evaluations.find((e) => e.id === id) ?? notFound(`Evaluation ${id}`);
    case "POST /evaluations": return { started: false };

    default:
      throw new ApiError(`No demo data for ${method} ${path}`, 404, "not_found");
  }
}

/** Events to replay for a run's live timeline in demo mode. */
export function demoRunEvents(runId: number) {
  const run = state.runs.find((r) => r.id === runId);
  return run ? data.runEvents(run) : [];
}

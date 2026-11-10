/**
 * Typed API client. Types are generated from the FastAPI OpenAPI schema
 * (`npm run gen:api`), so request/response shapes stay in sync with the backend.
 *
 * All requests go to the same origin (proxied to FastAPI by next.config.ts). No secret is
 * ever stored in client state: the optional session cookie is HttpOnly.
 */
import type { components } from "@/types/api";

export type Schemas = components["schemas"];
export type RunOut = Schemas["RunOut"];
export type RunDetail = Schemas["RunDetail"];
export type StepOut = Schemas["StepOut"];
export type PatchOut = Schemas["PatchOut"];
export type TestExecutionOut = Schemas["TestExecutionOut"];
export type SentryIssueOut = Schemas["SentryIssueOut"];
export type SentryIssueDetail = Schemas["SentryIssueDetail"];
export type RepositoryOut = Schemas["RepositoryOut"];
export type RemoteRepositoryOut = Schemas["RemoteRepositoryOut"];
export type MappingIn = Schemas["MappingIn"];
export type ApprovalOut = Schemas["ApprovalOut"];
export type PullRequestOut = Schemas["PullRequestOut"];
export type IntegrationOut = Schemas["IntegrationOut"];
export type ConnectionTestResult = Schemas["ConnectionTestResult"];
export type SettingsOut = Schemas["SettingsOut"];
export type PolicyReportOut = Schemas["PolicyReportOut"];
export type NormalizedEvent = Schemas["NormalizedEvent"];
export type TestCommand = Schemas["TestCommand"];
export type AutonomyMode = Schemas["AutonomyMode"];

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export type Json = Record<string, unknown>;

export interface DashboardSummary {
  demo: boolean;
  issues_ingested: number;
  issues_investigated: number;
  runs_total: number;
  runs_active: number;
  runs_failed: number;
  runs_needing_review: number;
  successful_validations: number;
  awaiting_approval: number;
  open_pull_requests: number;
  abstentions: number;
  outcomes: Record<string, number>;
}

export interface ActivityItem {
  id: number;
  action: string;
  actor: string;
  outcome: string;
  resource: string;
  created_at: string;
  details: Json;
}

export interface Diagnostics {
  app_env: string;
  auth_mode: string;
  integrations: Record<string, { configured: boolean; mode: string | null; last_ok: boolean | null }>;
  groq: { model: string | null; structured_output_mode: string; max_input_tokens: number };
  sandbox: { backend: string; isolation: string; available: boolean; detail: string; code_execution_enabled: boolean };
  faiss: { enabled: boolean; installed: boolean; detail: string };
  autonomy_mode: string;
  auto_create_pull_requests: boolean;
  app_secret_key_configured: boolean;
  workers: { running: boolean };
}

export interface Me {
  id: number;
  external_id: string;
  display_name: string;
  role: "admin" | "reviewer" | "viewer";
  auth_mode: "local" | "token";
}

export interface DemoScenario {
  key: string;
  description: string;
  issue_title: string;
  short_id: string;
}

export interface AuditEntry {
  id: number;
  actor_type: string;
  actor_id: string | null;
  action: string;
  resource_type: string;
  resource_id: string | null;
  outcome: string;
  details: Json;
  created_at: string;
}

export interface EvaluationSummary {
  id: number;
  dataset_version: string;
  model_name: string;
  llm_mode: string;
  config_version: string;
  status: string;
  metrics: Record<string, number | boolean | null>;
  started_at: string;
  completed_at: string | null;
  results?: Json[];
}

export interface RemoteIssue {
  sentry_issue_id: string;
  short_id: string | null;
  project: string;
  title: string;
  culprit: string | null;
  exception_type: string | null;
  level: string | null;
  status: string | null;
  permalink: string | null;
  first_seen: string | null;
  last_seen: string | null;
  event_count: number;
  ingested_id: number | null;
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code: string,
    readonly details: Json = {},
  ) {
    super(message);
  }
}

type Query = Record<string, string | number | boolean | null | undefined>;

function qs(query?: Query): string {
  if (!query) return "";
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined && value !== null && value !== "") params.set(key, String(value));
  }
  const text = params.toString();
  return text ? `?${text}` : "";
}

/**
 * Frontend-only demo build: when true, every request is answered in the browser from dummy data
 * (lib/demo) and no backend is needed. Inlined at build time, so the mock is dropped from normal builds.
 */
export const DEMO_MODE = process.env.NEXT_PUBLIC_DEMO_MODE === "true";

async function request<T>(method: string, path: string, body?: unknown, query?: Query): Promise<T> {
  if (DEMO_MODE) {
    const { demoRequest } = await import("@/lib/demo/handler");
    return demoRequest<T>(method, path, body, query);
  }
  const response = await fetch(`/api/v1${path}${qs(query)}`, {
    method,
    credentials: "same-origin",
    cache: "no-store",
    headers: {
      Accept: "application/json",
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
      // Required by the backend's CSRF check for state-changing requests.
      "X-Requested-With": "patchora",
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!response.ok) {
    let code = "http_error";
    let message = `Request failed (${response.status})`;
    let details: Json = {};
    try {
      const payload = (await response.json()) as { error?: { code: string; message: string; details?: Json } };
      if (payload.error) {
        code = payload.error.code;
        message = payload.error.message;
        details = payload.error.details ?? {};
      }
    } catch {
      // non-JSON error body
    }
    throw new ApiError(message, response.status, code, details);
  }
  if (response.status === 204) return undefined as T;
  const type = response.headers.get("content-type") ?? "";
  if (type.includes("application/json")) return (await response.json()) as T;
  return (await response.text()) as T;
}

const get = <T>(path: string, query?: Query) => request<T>("GET", path, undefined, query);
const post = <T>(path: string, body?: unknown, query?: Query) => request<T>("POST", path, body ?? {}, query);
const put = <T>(path: string, body: unknown) => request<T>("PUT", path, body);
const patch = <T>(path: string, body: unknown) => request<T>("PATCH", path, body);
const del = <T>(path: string) => request<T>("DELETE", path);

export const api = {
  // health & auth
  diagnostics: () => get<Diagnostics>("/diagnostics"),
  me: () => get<Me>("/auth/me"),
  login: (token: string) => post<{ display_name: string; role: string }>("/auth/login", { token }),
  logout: () => post<{ ok: boolean }>("/auth/logout"),

  // dashboard
  summary: (demo: boolean) => get<DashboardSummary>("/dashboard/summary", { demo }),
  activity: (limit = 15) => get<ActivityItem[]>("/dashboard/activity", { limit }),

  // integrations
  integrations: () => get<IntegrationOut[]>("/integrations"),
  testIntegration: (provider: string) => post<ConnectionTestResult>(`/integrations/${provider}/test`),
  saveCredentials: (provider: string, values: Record<string, string | null>) =>
    put<IntegrationOut>(`/integrations/${provider}/credentials`, { values }),
  groqModels: () => get<{ models: { id: string; active: boolean | null; context_window: number | null }[] }>(
    "/integrations/groq/models"),

  // sentry issues
  issues: (query: Query) => get<Page<SentryIssueOut>>("/sentry/issues", query),
  issue: (id: number, live = false) => get<SentryIssueDetail>(`/sentry/issues/${id}`, { live }),
  remoteIssues: (query: Query) => get<{ items: RemoteIssue[]; next_cursor: string | null }>(
    "/sentry/remote-issues", query),
  importIssue: (sentryIssueId: string) =>
    post<SentryIssueOut>("/sentry/issues/import", undefined, { sentry_issue_id: sentryIssueId }),
  analyzeIssue: (id: number, body: { autonomy_mode?: string; repository_id?: number | null }) =>
    post<RunOut>(`/sentry/issues/${id}/analyze`, body),
  linkRepository: (id: number, repositoryId: number | null) =>
    post<SentryIssueOut>(`/sentry/issues/${id}/repository`, { repository_id: repositoryId }),

  // repositories
  repositories: (query?: Query) => get<Page<RepositoryOut>>("/repositories", { limit: 100, ...query }),
  remoteRepositories: () => get<RemoteRepositoryOut[]>("/repositories/remote"),
  connectRepository: (fullName: string, enabled: boolean) =>
    post<RepositoryOut>("/repositories", { full_name: fullName, enabled }),
  updateRepository: (id: number, body: { enabled?: boolean; test_commands?: TestCommand[] }) =>
    patch<RepositoryOut>(`/repositories/${id}`, body),
  testRepository: (id: number) => post<ConnectionTestResult>(`/repositories/${id}/test-connection`),
  syncRepository: (id: number) => post<RepositoryOut>(`/repositories/${id}/sync`),
  addMapping: (id: number, body: MappingIn) => post<Schemas["MappingOut"]>(`/repositories/${id}/mappings`, body),
  deleteMapping: (mappingId: number) => del<void>(`/repositories/mappings/${mappingId}`),

  // runs
  runs: (query: Query) => get<Page<RunOut>>("/runs", query),
  run: (id: number) => get<RunDetail>(`/runs/${id}`),
  retryRun: (id: number) => post<RunOut>(`/runs/${id}/retry`),
  cancelRun: (id: number) => post<RunOut>(`/runs/${id}/cancel`),
  diff: (id: number, attempt?: number) => get<string>(`/runs/${id}/diff`, { attempt }),
  validatePatch: (id: number) => post<PolicyReportOut>(`/runs/${id}/validate`),
  createGithubIssue: (id: number) => post<{ created: boolean; url: string; number: number }>(
    `/runs/${id}/github-issue`),
  requestPullRequest: (id: number) => post<{ status: string; url?: string }>(`/runs/${id}/pull-request`),

  // approvals & pull requests
  approvals: (query: Query) => get<Page<ApprovalOut>>("/approvals", query),
  approve: (id: number, reason: string) =>
    post<ApprovalOut>(`/approvals/${id}/approve`, { confirm: true, reason: reason || null }),
  reject: (id: number, reason: string) =>
    post<ApprovalOut>(`/approvals/${id}/reject`, { confirm: true, reason: reason || null }),
  pullRequests: (query: Query) => get<Page<PullRequestOut>>("/pull-requests", query),
  refreshPullRequest: (id: number) => get<PullRequestOut>(`/pull-requests/${id}`, { refresh: true }),

  // settings, audit
  settings: () => get<SettingsOut>("/settings"),
  updateSettings: (values: Json) => put<SettingsOut>("/settings", { values }),
  audit: (query: Query) => get<Page<AuditEntry>>("/audit", query),

  // demo
  demoScenarios: () => get<DemoScenario[]>("/demo/scenarios"),
  seedDemo: () => post<Json>("/demo/seed"),
  startDemo: (scenario: string, autonomyMode: string, useRealLlm: boolean) =>
    post<{ run_id: number; created: boolean }>("/demo/runs", {
      scenario, autonomy_mode: autonomyMode, use_real_llm: useRealLlm }),
  resetDemo: () => post<Record<string, number>>("/demo/reset"),

  // evaluations
  evaluations: () => get<Page<EvaluationSummary>>("/evaluations"),
  evaluation: (id: number) => get<EvaluationSummary>(`/evaluations/${id}`),
  startEvaluation: () => post<{ started: boolean }>("/evaluations"),
};

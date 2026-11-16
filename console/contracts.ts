export type Person = {
  id: string; tenant_id: string; name: string; email: string; role: "operator" | "manager" | "viewer";
};

export type Run = {
  id: string; goal: string; mode: string; status: string; created_at: number; updated_at: number;
  deadline: number; control: string; user_id: string;
  spec: { kind: string; apps: string[]; max_actions: number; approval_required: boolean };
  summary: { actions: number; verified: number; discovered: number; model_tokens: number; stop_reason: string | null };
};

export type RunList = { items: Run[]; total: number; status_counts: Record<string, number> };
export type Event = {
  event_id: string; sequence: number; timestamp: string; event_type: string;
  state_before: string; state_after: string;
  payload: {
    url?: string; evidence?: string; resource_id?: string; method?: string; code?: string;
    reason?: string; intent_id?: string; operation?: string;
    verification?: { tier: string; predicates: Record<string, boolean> };
  };
};

export type Approval = {
  id: string; run_id: string; intent_id: string; requested_by: string; expires: number;
  details: {
    operation: string; resource_id: string; before: Record<string, unknown>;
    requested: Record<string, unknown>; evidence_id?: string;
  };
};

export type BenchCase = {
  id: string; mode: string; status: string; layout: string; family: string;
  task_success: boolean | null; wall_ms?: number; failure_tags: string[];
};

export type Benchmark = {
  id: string; status: string; options: { suite: string; mode: string; seed: number }; created_at: number;
  results?: {
    error?: string;
    cases?: BenchCase[];
    metrics?: Record<string, { n: number; successes: number; unavailable: number; wall_ms_p95: number | null }>;
  };
};

export type Settings = {
  model: string | null; demo_identity: boolean; evidence_retention_seconds: number;
  maximum_run_seconds: number; maximum_browser_actions: number;
};

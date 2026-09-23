/** Thin client for the swarmdeck control API. */

export interface Aggregates {
  total: number;
  by_status: Record<string, number>;
  running: number;
  done: number;
  pending: number;
  tokens_in: number;
  tokens_out: number;
  cost_usd: number;
  elapsed: number;
  throughput: number;
  mean_duration: number;
  peak_running: number;
  dropped_events: number;
}

export interface FeedEntry {
  ts: number;
  level: string;
  index: number;
  msg: string;
}

export interface SwarmSummary {
  swarm_id: string;
  name: string;
  phase: string;
  created_at: number;
  finished_at: number | null;
  agg: Aggregates;
}

export interface SpecEntry {
  path: string;
  name: string;
  description?: string;
  agents?: number;
  concurrency?: number;
  runtime?: string;
  command?: string;
  error?: string;
}

export interface AgentDetail {
  index: number;
  id: string;
  status: string;
  attempt: number;
  progress: number;
  pid: number | null;
  started_at: number | null;
  ended_at: number | null;
  duration: number;
  exit_code: number | null;
  label: string;
  error: string | null;
  result: unknown;
  tokens_in: number;
  tokens_out: number;
  cost_usd: number;
  metrics: Record<string, number>;
  edges: { to: string; kind: string }[];
  artifacts: { name: string; path: string | null }[];
  task: unknown;
  tags: string[];
  log_count: number;
  dropped_logs: number;
  logs: { ts: number; level: string; text: string }[];
}

export const TOKEN = new URLSearchParams(location.search).get("token") ?? "";

function headers(): HeadersInit {
  return TOKEN
    ? { "Content-Type": "application/json", Authorization: `Bearer ${TOKEN}` }
    : { "Content-Type": "application/json" };
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, { ...init, headers: headers() });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* response was not JSON; the status text is the best we have */
    }
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

export const api = {
  health: () => request<Record<string, unknown>>("/api/health"),
  specs: () => request<SpecEntry[]>("/api/specs"),
  swarms: () => request<SwarmSummary[]>("/api/swarms"),

  launch: (body: { spec_path?: string; concurrency?: number }) =>
    request<{ swarm_id: string; warnings: string[] }>("/api/swarms", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  agent: (swarm: string, index: number) =>
    request<AgentDetail>(`/api/swarms/${swarm}/agents/${index}`),

  graph: (swarm: string) =>
    request<{
      nodes: { id: number; status: string; label: string }[];
      links: { source: number; target: number; kind: string }[];
    }>(`/api/swarms/${swarm}/graph`),

  control: (
    swarm: string,
    body: {
      action: string;
      indices?: number[];
      status?: string;
      tag?: string;
      value?: number;
    },
  ) =>
    request<Record<string, number>>(`/api/swarms/${swarm}/control`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  remove: (swarm: string) =>
    request<{ ok: boolean }>(`/api/swarms/${swarm}`, { method: "DELETE" }),
};

export function wsUrl(swarmId: string): string {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const qs = TOKEN ? `?token=${encodeURIComponent(TOKEN)}` : "";
  return `${proto}//${location.host}/ws/swarms/${swarmId}${qs}`;
}

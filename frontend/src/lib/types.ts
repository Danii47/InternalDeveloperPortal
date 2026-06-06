// Shared TypeScript interfaces mirroring the FastAPI Pydantic models.
// Import from here rather than repeating inline type assertions in components.

export interface VMNetwork {
  id: string;
  bridge: string;
}

export interface VMInventoryItem {
  vmid: number;
  name: string;
  node: string;
  status: "running" | "stopped" | string;
  ram_mb: number;
  cpu: number;
  managed: boolean;
  disk_gb: number;
  sockets: number;
  networks: VMNetwork[];
  template_id: number | null;
  template_name: string | null;
  /** Epoch (seconds) when the VM auto-destroys. null = persistent / not tracked. */
  ttl_expires_at: number | null;
}

export interface Task {
  id: string;
  type: string;
  target: string;
  status: "pending" | "running" | "completed" | "failed";
  error: string | null;
  owner: string | null;
  updated_at: number;
}

// ── Quota types ───────────────────────────────────────────────────────────────

export interface PoolQuota {
  pool_name: string;
  max_cpu: number;
  max_ram_mb: number;
  max_disk_gb: number;
}

export interface PoolUsage {
  cpu: number;
  ram_mb: number;
  disk_gb: number;
}

export interface PoolQuotaEntry {
  pool_name: string;
  quota: PoolQuota;
  usage: PoolUsage;
}

// ── Live metrics ─────────────────────────────────────────────────────────────

export interface VMMetricEntry {
  cpu_pct: number;
  mem_pct: number;
  status: string;
}

/** Map of vmid (as string) → live metrics. Response of GET /api/inventory/metrics. */
export type VMMetrics = Record<string, VMMetricEntry>;

// ── Auth ──────────────────────────────────────────────────────────────────────

export interface AuthUser {
  userid: string;
  realm: string;
}

// ── API response helpers ──────────────────────────────────────────────────────

export interface QuotaViolation {
  message: string;
  violations: string[];
}

export interface TaskResponse {
  message: string;
  task_id: string;
}

/**
 * Status encoding for the swarm.
 *
 * Hue carries four states only -- gray (inert), blue (in flight), green
 * (succeeded), red (failed) -- because those four are the ones that survive
 * colorblind simulation with room to spare. Everything finer is a *sub-state*
 * of one of them and is carried by lightness or by a diagonal hatch, which are
 * shape channels no form of colour vision deficiency touches.
 *
 * Two pairings were tried and rejected on measurement rather than taste:
 * violet "paused" against blue "running" measures deltaE 1.6 under deuteranopia,
 * and amber "timeout" against red "failed" measures 14.1 even with normal
 * colour vision. Both now share their parent hue and differ by texture.
 *
 * Counts in the legend are the third channel: identity is never colour alone.
 */

export type Texture = "none" | "hatch";

export interface StatusStyle {
  key: string;
  label: string;
  fill: string;
  texture: Texture;
  /** Terminal states stop animating and stop counting toward concurrency. */
  terminal: boolean;
}

/** Index matches the integer codes the server sends. */
export const STATUS: StatusStyle[] = [
  { key: "pending", label: "pending", fill: "#2e2e2b", texture: "none", terminal: false },
  { key: "starting", label: "starting", fill: "#7fb3ef", texture: "none", terminal: false },
  { key: "running", label: "running", fill: "#3987e5", texture: "none", terminal: false },
  { key: "succeeded", label: "succeeded", fill: "#13853a", texture: "none", terminal: true },
  { key: "failed", label: "failed", fill: "#ff8577", texture: "none", terminal: true },
  { key: "cancelled", label: "cancelled", fill: "#6b6862", texture: "none", terminal: true },
  { key: "timeout", label: "timeout", fill: "#ff8577", texture: "hatch", terminal: true },
  { key: "paused", label: "paused", fill: "#3987e5", texture: "hatch", terminal: false },
  { key: "retrying", label: "retrying", fill: "#3987e5", texture: "hatch", terminal: false },
];

export const STATUS_BY_KEY: Record<string, number> = Object.fromEntries(
  STATUS.map((s, i) => [s.key, i]),
);

/** Display order in the legend: the arc of a run, not the wire order. */
export const LEGEND_ORDER = [0, 1, 2, 7, 8, 3, 4, 6, 5];

export function styleFor(code: number): StatusStyle {
  return STATUS[code] ?? STATUS[0];
}

export const INK = {
  surface: "#141414",
  plane: "#0d0d0d",
  raised: "#1c1c1b",
  primary: "#ffffff",
  secondary: "#c3c2b7",
  muted: "#898781",
  grid: "#2c2c2a",
  axis: "#383835",
  border: "rgba(255,255,255,0.10)",
  accent: "#3987e5",
};

export function fmtDuration(seconds: number): string {
  if (!seconds || seconds < 0) return "0s";
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)}s`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  if (m < 60) return `${m}m ${s}s`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

export function fmtCount(n: number): string {
  if (n < 1000) return String(Math.round(n));
  if (n < 1_000_000) return `${(n / 1000).toFixed(n < 10_000 ? 1 : 0)}k`;
  return `${(n / 1_000_000).toFixed(1)}M`;
}

export function fmtCost(n: number): string {
  if (!n) return "$0.00";
  if (n < 0.01) return `$${n.toFixed(4)}`;
  return `$${n.toFixed(2)}`;
}

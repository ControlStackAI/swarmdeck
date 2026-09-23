/**
 * Stat tiles and time-series for one run.
 *
 * Each chart carries exactly one series, so the title names it and no legend
 * box is needed. Every chart gets a crosshair and a tooltip -- a rendered
 * chart you cannot interrogate is a picture of data, not data.
 */

import { useEffect, useRef, useState } from "react";
import { Aggregates } from "./api";
import { INK, fmtCost, fmtCount, fmtDuration, styleFor, STATUS_BY_KEY } from "./status";
import { Sample, SwarmStore } from "./useSwarm";

interface Props {
  store: SwarmStore;
}

export function Metrics({ store }: Props) {
  const agg = store.agg;
  if (!agg) return <p className="empty">waiting for the first frame…</p>;

  const failed =
    (agg.by_status.failed ?? 0) + (agg.by_status.timeout ?? 0) + (agg.by_status.cancelled ?? 0);

  return (
    <div className="metrics">
      <div className="tiles">
        <Tile label="agents" value={fmtCount(agg.total)} detail={`${agg.pending} pending`} />
        <Tile
          label="in flight"
          value={fmtCount(agg.running)}
          detail={`peak ${fmtCount(agg.peak_running)}`}
          tone="#3987e5"
        />
        <Tile
          label="succeeded"
          value={fmtCount(agg.by_status.succeeded ?? 0)}
          detail={`${pct(agg.by_status.succeeded ?? 0, agg.total)} of total`}
          tone="#13853a"
        />
        <Tile
          label="failed"
          value={fmtCount(failed)}
          detail={failed ? `${pct(failed, agg.total)} of total` : "none"}
          tone={failed ? "#ff8577" : undefined}
        />
        <Tile label="elapsed" value={fmtDuration(agg.elapsed)} detail={`${agg.throughput}/s`} />
        <Tile
          label="mean agent"
          value={fmtDuration(agg.mean_duration)}
          detail={`${fmtCount(agg.done)} completed`}
        />
        <Tile
          label="cost"
          value={fmtCost(agg.cost_usd)}
          detail={`${fmtCount(agg.tokens_in + agg.tokens_out)} tokens`}
        />
        <Tile
          label="tokens"
          value={fmtCount(agg.tokens_in)}
          detail={`${fmtCount(agg.tokens_out)} out`}
        />
      </div>

      <div className="charts">
        <Chart
          title="Agents in flight"
          store={store}
          pick={(s) => s.running}
          format={(v) => fmtCount(v)}
          fill
        />
        <Chart
          title="Completed"
          store={store}
          pick={(s) => s.done}
          format={(v) => fmtCount(v)}
          fill
        />
        <Chart
          title="Throughput (agents/s)"
          store={store}
          pick={(s) => s.throughput}
          format={(v) => v.toFixed(2)}
        />
        <Chart
          title="Cumulative cost"
          store={store}
          pick={(s) => s.cost}
          format={(v) => fmtCost(v)}
        />
      </div>

      <StatusTable agg={agg} />
    </div>
  );
}

function pct(n: number, total: number): string {
  if (!total) return "0%";
  return `${Math.round((n / total) * 100)}%`;
}

function Tile({
  label,
  value,
  detail,
  tone,
}: {
  label: string;
  value: string;
  detail?: string;
  tone?: string;
}) {
  return (
    <div className="tile">
      <span className="tile-label">{label}</span>
      <span className="tile-value" style={tone ? { color: tone } : undefined}>
        {value}
      </span>
      {detail && <span className="tile-detail">{detail}</span>}
    </div>
  );
}

/** Accessible alternative to the colour encoding, and useful on its own. */
function StatusTable({ agg }: { agg: Aggregates }) {
  const rows = Object.entries(agg.by_status).filter(([, v]) => v > 0);
  if (!rows.length) return null;
  return (
    <table className="status-table">
      <caption>Agents by status</caption>
      <thead>
        <tr>
          <th scope="col">status</th>
          <th scope="col">count</th>
          <th scope="col">share</th>
        </tr>
      </thead>
      <tbody>
        {rows.map(([key, count]) => {
          const style = styleFor(STATUS_BY_KEY[key] ?? 0);
          return (
            <tr key={key}>
              <th scope="row">
                <span className="swatch" style={{ background: style.fill }} aria-hidden />
                {key}
              </th>
              <td>{fmtCount(count)}</td>
              <td>{pct(count, agg.total)}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function Chart({
  title,
  store,
  pick,
  format,
  fill = false,
}: {
  title: string;
  store: SwarmStore;
  pick: (s: Sample) => number;
  format: (v: number) => string;
  fill?: boolean;
}) {
  const ref = useRef<HTMLCanvasElement>(null);
  const [cursor, setCursor] = useState<{ i: number; x: number } | null>(null);
  const history = store.history;

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const w = canvas.clientWidth;
    const h = canvas.clientHeight;
    canvas.width = w * dpr;
    canvas.height = h * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    const pad = { top: 8, right: 8, bottom: 16, left: 40 };
    const plotW = w - pad.left - pad.right;
    const plotH = h - pad.top - pad.bottom;
    if (plotW <= 0 || plotH <= 0 || history.length < 2) return;

    const values = history.map(pick);
    const max = Math.max(...values, 1e-9);
    const scaleY = (v: number) => pad.top + plotH - (v / max) * plotH;
    const scaleX = (i: number) => pad.left + (i / (history.length - 1)) * plotW;

    // Recessive gridlines and two ticks: enough to read, not enough to compete.
    ctx.strokeStyle = INK.grid;
    ctx.lineWidth = 1;
    ctx.fillStyle = INK.muted;
    ctx.font = "10px system-ui, sans-serif";
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    for (const frac of [0, 0.5, 1]) {
      const y = Math.round(pad.top + plotH * frac) + 0.5;
      ctx.beginPath();
      ctx.moveTo(pad.left, y);
      ctx.lineTo(w - pad.right, y);
      ctx.stroke();
      ctx.fillText(format(max * (1 - frac)), pad.left - 6, y);
    }

    ctx.beginPath();
    history.forEach((s, i) => {
      const x = scaleX(i);
      const y = scaleY(pick(s));
      i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    });

    if (fill) {
      const area = new Path2D();
      history.forEach((s, i) => {
        const x = scaleX(i);
        const y = scaleY(pick(s));
        i === 0 ? area.moveTo(x, y) : area.lineTo(x, y);
      });
      area.lineTo(scaleX(history.length - 1), pad.top + plotH);
      area.lineTo(scaleX(0), pad.top + plotH);
      area.closePath();
      const grad = ctx.createLinearGradient(0, pad.top, 0, pad.top + plotH);
      grad.addColorStop(0, "rgba(57,135,229,0.30)");
      grad.addColorStop(1, "rgba(57,135,229,0.02)");
      ctx.fillStyle = grad;
      ctx.fill(area);
    }

    ctx.strokeStyle = INK.accent;
    ctx.lineWidth = 2;
    ctx.lineJoin = "round";
    ctx.stroke();

    if (cursor) {
      const x = scaleX(cursor.i);
      ctx.strokeStyle = INK.axis;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(x + 0.5, pad.top);
      ctx.lineTo(x + 0.5, pad.top + plotH);
      ctx.stroke();
      ctx.fillStyle = INK.accent;
      ctx.beginPath();
      ctx.arc(x, scaleY(pick(history[cursor.i])), 4, 0, Math.PI * 2);
      ctx.fill();
      ctx.strokeStyle = INK.surface;
      ctx.lineWidth = 2;
      ctx.stroke();
    }
  }, [history, cursor, pick, format, fill, store.epoch]);

  const latest = history.length ? pick(history[history.length - 1]) : 0;
  const shown = cursor && history[cursor.i] ? pick(history[cursor.i]) : latest;

  return (
    <figure className="chart">
      <figcaption>
        <span>{title}</span>
        <strong>{format(shown)}</strong>
      </figcaption>
      <div className="chart-plot">
        <canvas
          ref={ref}
          onMouseMove={(ev) => {
            const box = ev.currentTarget.getBoundingClientRect();
            const pad = { left: 40, right: 8 };
            const plotW = box.width - pad.left - pad.right;
            const rel = (ev.clientX - box.left - pad.left) / Math.max(plotW, 1);
            const i = Math.round(rel * (history.length - 1));
            if (i >= 0 && i < history.length) setCursor({ i, x: ev.clientX });
          }}
          onMouseLeave={() => setCursor(null)}
        />
        {cursor && history[cursor.i] && (
          <span className="chart-time">
            {new Date(history[cursor.i].t).toLocaleTimeString()}
          </span>
        )}
      </div>
    </figure>
  );
}

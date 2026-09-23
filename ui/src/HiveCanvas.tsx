/**
 * The hive: one cell per agent, on a canvas.
 *
 * A thousand DOM nodes each transitioning a background colour ten times a
 * second is what kills every naive version of this view. A canvas draws the
 * same thousand cells in well under a millisecond and does not care whether
 * there are ten of them or ten thousand.
 */

import { useEffect, useRef, useState } from "react";
import { INK, styleFor } from "./status";
import { SwarmStore } from "./useSwarm";

const MIN_CELL = 3;
const MAX_CELL = 44;

interface Props {
  store: SwarmStore;
  selected: Set<number>;
  onSelect: (indices: number[], additive: boolean) => void;
  filter: number | null;
  onInspect: (index: number) => void;
}

interface Layout {
  pitch: number;
  cell: number;
  cols: number;
  rows: number;
  originX: number;
}

function computeLayout(w: number, h: number, n: number): Layout {
  if (n <= 0 || w <= 0 || h <= 0) {
    return { pitch: MIN_CELL, cell: MIN_CELL, cols: 1, rows: 1, originX: 0 };
  }
  let lo = MIN_CELL;
  let hi = Math.min(w, h, MAX_CELL + 6);
  let pitch = MIN_CELL;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    const cols = Math.floor(w / mid);
    const rows = Math.floor(h / mid);
    if (cols > 0 && rows > 0 && cols * rows >= n) {
      pitch = mid;
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  const gap = Math.max(1, Math.min(3, Math.round(pitch * 0.14)));
  const cols = Math.max(1, Math.floor(w / pitch));
  const rows = Math.ceil(n / cols);
  // Centre the grid so a part-filled last row does not look like a bug.
  const originX = Math.max(0, Math.floor((w - cols * pitch) / 2));
  return { pitch, cell: Math.max(2, pitch - gap), cols, rows, originX };
}

export function HiveCanvas({ store, selected, onSelect, filter, onInspect }: Props) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const layoutRef = useRef<Layout>({ pitch: 8, cell: 7, cols: 1, rows: 1, originX: 0 });
  const [hover, setHover] = useState<{ index: number; x: number; y: number } | null>(null);
  const dragRef = useRef<{ start: number; additive: boolean } | null>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    const wrap = wrapRef.current;
    if (!canvas || !wrap) return;
    const ctx = canvas.getContext("2d", { alpha: false });
    if (!ctx) return;

    let raf = 0;
    let lastEpoch = -1;
    let lastKey = "";

    const draw = () => {
      raf = requestAnimationFrame(draw);

      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const w = wrap.clientWidth;
      const h = wrap.clientHeight;
      const key = `${w}x${h}|${store.total}|${filter}|${selected.size}|${hover?.index ?? -1}`;
      if (store.epoch === lastEpoch && key === lastKey) return;
      lastEpoch = store.epoch;
      lastKey = key;

      if (canvas.width !== w * dpr || canvas.height !== h * dpr) {
        canvas.width = Math.max(1, w * dpr);
        canvas.height = Math.max(1, h * dpr);
        canvas.style.width = `${w}px`;
        canvas.style.height = `${h}px`;
      }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.fillStyle = INK.surface;
      ctx.fillRect(0, 0, w, h);

      const n = store.total;
      if (!n) return;
      const lay = computeLayout(w, h, n);
      layoutRef.current = lay;
      const { pitch, cell, cols, originX } = lay;
      const radius = cell >= 10 ? 2 : 0;
      const showProgress = cell >= 6;

      for (let i = 0; i < n; i++) {
        const code = store.status[i];
        const style = styleFor(code);
        const x = originX + (i % cols) * pitch;
        const y = Math.floor(i / cols) * pitch;

        const dimmed = filter !== null && code !== filter;
        ctx.globalAlpha = dimmed ? 0.12 : 1;

        // Terminal cells are flat. In-flight cells show a base wash with the
        // completed fraction filled from the bottom, so a stalled agent is
        // visible as a cell that stops climbing.
        if (!style.terminal && code !== 0 && showProgress) {
          const p = Math.max(0, Math.min(1, store.progress[i]));
          ctx.fillStyle = style.fill;
          ctx.globalAlpha = dimmed ? 0.06 : 0.28;
          rect(ctx, x, y, cell, radius);
          ctx.globalAlpha = dimmed ? 0.12 : 1;
          if (p > 0) {
            const fh = Math.max(1, Math.round(cell * p));
            rect(ctx, x, y + cell - fh, cell, radius, fh);
          }
        } else {
          ctx.fillStyle = style.fill;
          rect(ctx, x, y, cell, radius);
        }

        if (style.texture === "hatch" && cell >= 5) {
          hatch(ctx, x, y, cell);
        }
      }

      ctx.globalAlpha = 1;

      if (selected.size && selected.size < n) {
        ctx.strokeStyle = INK.primary;
        ctx.lineWidth = 1;
        for (const i of selected) {
          if (i >= n) continue;
          const x = originX + (i % cols) * pitch;
          const y = Math.floor(i / cols) * pitch;
          ctx.strokeRect(x - 0.5, y - 0.5, cell + 1, cell + 1);
        }
      }

      if (hover && hover.index < n) {
        const x = originX + (hover.index % cols) * pitch;
        const y = Math.floor(hover.index / cols) * pitch;
        ctx.strokeStyle = INK.primary;
        ctx.lineWidth = 2;
        ctx.strokeRect(x - 1, y - 1, cell + 2, cell + 2);
      }
    };

    raf = requestAnimationFrame(draw);
    return () => cancelAnimationFrame(raf);
  }, [store, filter, selected, hover]);

  const indexAt = (ev: React.MouseEvent): number | null => {
    const canvas = canvasRef.current;
    if (!canvas) return null;
    const box = canvas.getBoundingClientRect();
    const { pitch, cols, originX, cell } = layoutRef.current;
    const lx = ev.clientX - box.left - originX;
    const ly = ev.clientY - box.top;
    if (lx < 0) return null;
    const col = Math.floor(lx / pitch);
    const row = Math.floor(ly / pitch);
    // Ignore the gutter between cells so hover does not flicker.
    if (lx - col * pitch > cell || ly - row * pitch > cell) return null;
    if (col < 0 || col >= cols || row < 0) return null;
    const index = row * cols + col;
    return index >= 0 && index < store.total ? index : null;
  };

  return (
    <div className="hive" ref={wrapRef}>
      <canvas
        ref={canvasRef}
        onMouseMove={(ev) => {
          const index = indexAt(ev);
          setHover(index === null ? null : { index, x: ev.clientX, y: ev.clientY });
          const drag = dragRef.current;
          if (drag && index !== null) {
            const [lo, hi] = drag.start < index ? [drag.start, index] : [index, drag.start];
            const range: number[] = [];
            for (let i = lo; i <= hi; i++) range.push(i);
            onSelect(range, drag.additive);
          }
        }}
        onMouseLeave={() => {
          setHover(null);
          dragRef.current = null;
        }}
        onMouseDown={(ev) => {
          const index = indexAt(ev);
          if (index === null) {
            onSelect([], false);
            return;
          }
          dragRef.current = { start: index, additive: ev.shiftKey || ev.metaKey };
          onSelect([index], ev.shiftKey || ev.metaKey);
        }}
        onMouseUp={() => {
          dragRef.current = null;
        }}
        onDoubleClick={(ev) => {
          const index = indexAt(ev);
          if (index !== null) onInspect(index);
        }}
      />
      {hover && hover.index < store.total && (
        <HoverCard store={store} index={hover.index} x={hover.x} y={hover.y} />
      )}
      {store.total === 0 && <p className="hive-empty">no agents yet</p>}
    </div>
  );
}

function rect(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  size: number,
  radius: number,
  height = size,
) {
  if (radius > 0) {
    ctx.beginPath();
    ctx.roundRect(x, y, size, height, radius);
    ctx.fill();
  } else {
    ctx.fillRect(x, y, size, height);
  }
}

/** Diagonal hatch: the colourblind-safe channel for paused and timed-out. */
function hatch(ctx: CanvasRenderingContext2D, x: number, y: number, size: number) {
  ctx.save();
  ctx.beginPath();
  ctx.rect(x, y, size, size);
  ctx.clip();
  ctx.strokeStyle = INK.surface;
  ctx.lineWidth = Math.max(1, size / 7);
  const step = Math.max(3, size / 2.5);
  for (let o = -size; o < size * 2; o += step) {
    ctx.beginPath();
    ctx.moveTo(x + o, y);
    ctx.lineTo(x + o + size, y + size);
    ctx.stroke();
  }
  ctx.restore();
}

function HoverCard({
  store,
  index,
  x,
  y,
}: {
  store: SwarmStore;
  index: number;
  x: number;
  y: number;
}) {
  const style = styleFor(store.status[index]);
  const exit = store.exitCode(index);
  const flip = x > window.innerWidth - 240;
  return (
    <div
      className="hovercard"
      style={{ left: flip ? x - 232 : x + 14, top: Math.min(y + 14, window.innerHeight - 150) }}
    >
      <div className="hovercard-head">
        <span className="swatch" style={{ background: style.fill }} aria-hidden />
        <strong>a{String(index).padStart(5, "0")}</strong>
        <span className="muted">{style.label}</span>
      </div>
      <dl>
        <dt>progress</dt>
        <dd>{Math.round(store.progress[index] * 100)}%</dd>
        <dt>elapsed</dt>
        <dd>{store.duration[index].toFixed(1)}s</dd>
        {store.attempt[index] > 0 && (
          <>
            <dt>attempt</dt>
            <dd>{store.attempt[index] + 1}</dd>
          </>
        )}
        {store.cost[index] > 0 && (
          <>
            <dt>cost</dt>
            <dd>${store.cost[index].toFixed(4)}</dd>
          </>
        )}
        {exit !== null && (
          <>
            <dt>exit</dt>
            <dd>{exit}</dd>
          </>
        )}
      </dl>
      {store.label[index] && <p className="hovercard-label">{store.label[index]}</p>}
      <p className="hovercard-hint">double-click to inspect</p>
    </div>
  );
}

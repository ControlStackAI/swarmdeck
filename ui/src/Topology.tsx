/**
 * Delegation topology.
 *
 * Drawn only from edges agents actually declared, so an empty view is
 * information -- it means nothing in this swarm delegated, not that the view
 * is broken. The force layout is ~40 lines of Barnes-Hut-free n-body, which
 * is fine because only agents *with* edges are ever nodes.
 */

import { useEffect, useRef, useState } from "react";
import { api } from "./api";
import { INK, STATUS_BY_KEY, styleFor } from "./status";

interface Node {
  id: number;
  status: string;
  label: string;
  x: number;
  y: number;
  vx: number;
  vy: number;
}

interface Link {
  source: number;
  target: number;
  kind: string;
}

const MAX_NODES = 600;

export function Topology({ swarmId }: { swarmId: string }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [state, setState] = useState<{ nodes: Node[]; links: Link[] } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [truncated, setTruncated] = useState(0);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const graph = await api.graph(swarmId);
        if (!alive) return;
        const connected = new Set<number>();
        for (const l of graph.links) {
          connected.add(l.source);
          connected.add(l.target);
        }
        let nodes = graph.nodes.filter((n) => connected.has(n.id));
        setTruncated(Math.max(0, nodes.length - MAX_NODES));
        nodes = nodes.slice(0, MAX_NODES);
        const keep = new Set(nodes.map((n) => n.id));
        setState({
          nodes: nodes.map((n, i) => ({
            ...n,
            // Seed on a ring so the layout unfolds instead of exploding.
            x: Math.cos((i / nodes.length) * Math.PI * 2) * 160,
            y: Math.sin((i / nodes.length) * Math.PI * 2) * 160,
            vx: 0,
            vy: 0,
          })),
          links: graph.links.filter((l) => keep.has(l.source) && keep.has(l.target)),
        });
        setError(null);
      } catch (e) {
        if (alive) setError((e as Error).message);
      }
    };
    load();
    const id = window.setInterval(load, 4000);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, [swarmId]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !state || state.nodes.length === 0) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const byId = new Map(state.nodes.map((n) => [n.id, n]));
    let raf = 0;
    let alpha = 1;

    const tick = () => {
      raf = requestAnimationFrame(tick);
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const w = canvas.clientWidth;
      const h = canvas.clientHeight;
      if (canvas.width !== w * dpr) {
        canvas.width = w * dpr;
        canvas.height = h * dpr;
      }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.fillStyle = INK.surface;
      ctx.fillRect(0, 0, w, h);

      const nodes = state.nodes;
      if (alpha > 0.005) {
        // Repulsion
        for (let i = 0; i < nodes.length; i++) {
          for (let j = i + 1; j < nodes.length; j++) {
            const a = nodes[i];
            const b = nodes[j];
            let dx = b.x - a.x;
            let dy = b.y - a.y;
            let d2 = dx * dx + dy * dy;
            if (d2 < 1) {
              dx = (Math.random() - 0.5) * 2;
              dy = (Math.random() - 0.5) * 2;
              d2 = 1;
            }
            const force = 900 / d2;
            const d = Math.sqrt(d2);
            const fx = (dx / d) * force;
            const fy = (dy / d) * force;
            a.vx -= fx;
            a.vy -= fy;
            b.vx += fx;
            b.vy += fy;
          }
        }
        // Springs + centring
        for (const l of state.links) {
          const a = byId.get(l.source);
          const b = byId.get(l.target);
          if (!a || !b) continue;
          const dx = b.x - a.x;
          const dy = b.y - a.y;
          const d = Math.max(1, Math.hypot(dx, dy));
          const force = (d - 70) * 0.02;
          const fx = (dx / d) * force;
          const fy = (dy / d) * force;
          a.vx += fx;
          a.vy += fy;
          b.vx -= fx;
          b.vy -= fy;
        }
        for (const n of nodes) {
          n.vx -= n.x * 0.004;
          n.vy -= n.y * 0.004;
          n.x += (n.vx *= 0.82) * alpha;
          n.y += (n.vy *= 0.82) * alpha;
        }
        alpha *= 0.99;
      }

      ctx.save();
      ctx.translate(w / 2, h / 2);

      ctx.strokeStyle = INK.axis;
      ctx.lineWidth = 1;
      for (const l of state.links) {
        const a = byId.get(l.source);
        const b = byId.get(l.target);
        if (!a || !b) continue;
        ctx.beginPath();
        ctx.moveTo(a.x, a.y);
        ctx.lineTo(b.x, b.y);
        ctx.stroke();
      }

      for (const n of nodes) {
        const style = styleFor(STATUS_BY_KEY[n.status] ?? 0);
        ctx.fillStyle = style.fill;
        ctx.beginPath();
        ctx.arc(n.x, n.y, 5, 0, Math.PI * 2);
        ctx.fill();
        ctx.strokeStyle = INK.surface;
        ctx.lineWidth = 2;
        ctx.stroke();
      }
      ctx.restore();
    };

    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [state]);

  if (error) return <p className="error">{error}</p>;
  if (!state) return <p className="empty">loading topology…</p>;
  if (!state.nodes.length) {
    return (
      <p className="empty">
        No delegation edges in this swarm.
        <br />
        <span className="muted">
          Agents draw edges by emitting <code>{'{"t": "edge", "to": "a00042"}'}</code>.
        </span>
      </p>
    );
  }

  return (
    <div className="topology">
      <canvas ref={canvasRef} />
      <p className="topology-meta muted">
        {state.nodes.length} connected agents · {state.links.length} edges
        {truncated > 0 && ` · ${truncated} more not drawn`}
      </p>
    </div>
  );
}

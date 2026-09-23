/**
 * Live swarm state over WebSocket.
 *
 * Agent state lives in typed arrays outside React, not in component state.
 * At 1000 agents and 10Hz, reconciling a thousand objects through React is
 * the difference between a smooth dashboard and a locked-up tab -- so React
 * renders the chrome at a human rate while the canvas reads the arrays
 * directly at frame rate.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { Aggregates, FeedEntry, wsUrl } from "./api";

const NO_EXIT = -2147483648;

/** History depth for the metrics charts: 5 minutes at 2Hz. */
const HISTORY_MAX = 600;

export interface Sample {
  t: number;
  running: number;
  done: number;
  failed: number;
  throughput: number;
  cost: number;
}

export class SwarmStore {
  total = 0;
  status = new Uint8Array(0);
  progress = new Float32Array(0);
  duration = new Float32Array(0);
  attempt = new Uint16Array(0);
  cost = new Float32Array(0);
  tokensIn = new Float32Array(0);
  tokensOut = new Float32Array(0);
  exit = new Int32Array(0);
  label: string[] = [];

  agg: Aggregates | null = null;
  feed: FeedEntry[] = [];
  phase = "created";
  name = "";
  /** Bumped whenever the canvas needs to repaint. */
  epoch = 0;

  history: Sample[] = [];
  private lastSample = 0;

  resize(total: number) {
    if (total === this.total) return;
    this.total = total;
    this.status = new Uint8Array(total);
    this.progress = new Float32Array(total);
    this.duration = new Float32Array(total);
    this.attempt = new Uint16Array(total);
    this.cost = new Float32Array(total);
    this.tokensIn = new Float32Array(total);
    this.tokensOut = new Float32Array(total);
    this.exit = new Int32Array(total).fill(NO_EXIT);
    this.label = new Array(total).fill("");
  }

  applyRows(rows: unknown[][]) {
    for (const row of rows) {
      const i = row[0] as number;
      if (i >= this.total) continue;
      this.status[i] = row[1] as number;
      this.progress[i] = row[2] as number;
      this.attempt[i] = row[3] as number;
      this.tokensIn[i] = row[4] as number;
      this.tokensOut[i] = row[5] as number;
      this.cost[i] = row[6] as number;
      this.duration[i] = row[7] as number;
      this.label[i] = (row[8] as string) ?? "";
      this.exit[i] = row[9] === null ? NO_EXIT : (row[9] as number);
    }
    this.epoch++;
  }

  applyMeta(agg: Aggregates | undefined, feed: FeedEntry[] | undefined, phase?: string) {
    if (agg) {
      this.agg = agg;
      this.sample(agg);
    }
    if (feed?.length) {
      this.feed = [...this.feed, ...feed].slice(-200);
    }
    if (phase) this.phase = phase;
  }

  private sample(agg: Aggregates) {
    const now = Date.now();
    if (now - this.lastSample < 500) return;
    this.lastSample = now;
    this.history.push({
      t: now,
      running: agg.running,
      done: agg.done,
      failed:
        (agg.by_status.failed ?? 0) +
        (agg.by_status.timeout ?? 0) +
        (agg.by_status.cancelled ?? 0),
      throughput: agg.throughput,
      cost: agg.cost_usd,
    });
    if (this.history.length > HISTORY_MAX) this.history.shift();
  }

  exitCode(i: number): number | null {
    return this.exit[i] === NO_EXIT ? null : this.exit[i];
  }

  reset() {
    this.history = [];
    this.feed = [];
    this.agg = null;
    this.total = 0;
    this.resize(0);
  }
}

export type ConnState = "connecting" | "open" | "closed" | "error";

export function useSwarm(swarmId: string | null) {
  const storeRef = useRef(new SwarmStore());
  const [conn, setConn] = useState<ConnState>("closed");
  const [, forceRender] = useState(0);
  const [dropped, setDropped] = useState(0);

  // React re-renders the chrome at 6Hz. The canvas is driven by its own
  // rAF loop off store.epoch, so it is not bound by this.
  useEffect(() => {
    const id = window.setInterval(() => forceRender((n) => n + 1), 160);
    return () => window.clearInterval(id);
  }, []);

  useEffect(() => {
    const store = storeRef.current;
    store.reset();
    if (!swarmId) {
      setConn("closed");
      return;
    }

    let ws: WebSocket | null = null;
    let retry: number | undefined;
    let attempt = 0;
    let disposed = false;

    const connect = () => {
      if (disposed) return;
      setConn("connecting");
      ws = new WebSocket(wsUrl(swarmId));

      ws.onopen = () => {
        attempt = 0;
        setConn("open");
      };

      ws.onmessage = (ev) => {
        const msg = JSON.parse(ev.data);
        switch (msg.type) {
          case "hello":
            store.name = msg.name;
            store.resize(msg.total);
            break;
          case "snapshot":
            store.name = msg.name ?? store.name;
            store.resize(msg.rows.length);
            store.applyRows(msg.rows);
            store.applyMeta(msg.agg, msg.feed, msg.phase);
            break;
          case "delta":
            store.applyRows(msg.rows);
            store.applyMeta(msg.agg, msg.feed, msg.phase);
            break;
          case "heartbeat":
            store.phase = msg.phase;
            break;
        }
        if (store.agg && store.agg.dropped_events !== dropped) {
          setDropped(store.agg.dropped_events);
        }
      };

      ws.onerror = () => setConn("error");

      ws.onclose = () => {
        if (disposed) return;
        setConn("closed");
        // A finished swarm closes cleanly; do not hammer the server for it.
        if (store.phase === "finished") return;
        attempt += 1;
        retry = window.setTimeout(connect, Math.min(500 * 2 ** attempt, 8000));
      };
    };

    connect();
    return () => {
      disposed = true;
      if (retry) window.clearTimeout(retry);
      ws?.close();
    };
    // `dropped` is read inside but must not re-open the socket.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [swarmId]);

  const store = storeRef.current;
  const refresh = useCallback(() => forceRender((n) => n + 1), []);
  return { store, conn, refresh };
}
